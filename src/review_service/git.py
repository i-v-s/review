from __future__ import annotations

import asyncio
import base64
import difflib
import json
import os
import stat
import tempfile
from pathlib import Path

from .config import Config
from .models import FileState, FileVersion, OperationRequest, ReviewError, Snapshot, digest, uid


def line_changes(old: FileVersion, new: FileVersion) -> list[dict]:
    a, b = old.bytes().splitlines(keepends=True), new.bytes().splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=a, b=b)
    groups = list(matcher.get_grouped_opcodes(3))
    rows = []
    for group_no, group in enumerate(groups):
        for tag, i, j, k, l in group:
            if tag == "equal":
                rows.extend(dict(kind="context", id=None, old=x + 1, new=k + x - i + 1,
                                 text=a[x].decode("utf-8").rstrip("\r\n"), group=group_no)
                            for x in range(i, j))
            else:
                rows.extend(dict(kind="delete", id=f"d:{x}", old=x + 1, new=None,
                                 text=a[x].decode("utf-8").rstrip("\r\n"), group=group_no)
                            for x in range(i, j))
                rows.extend(dict(kind="add", id=f"a:{x}", old=None, new=x + 1,
                                 text=b[x].decode("utf-8").rstrip("\r\n"), group=group_no)
                            for x in range(k, l))
    return rows


def select_changes(old: FileVersion, new: FileVersion, ids: list[str], whole: bool) -> FileVersion:
    if whole:
        return new.model_copy(deep=True)
    selected = set(ids)
    valid = {row["id"] for row in line_changes(old, new) if row["id"]}
    if not selected or not selected <= valid:
        raise ReviewError("Select changed lines from the displayed diff")
    a, b = old.bytes().splitlines(keepends=True), new.bytes().splitlines(keepends=True)
    result = []
    for tag, i, j, k, l in difflib.SequenceMatcher(a=a, b=b).get_opcodes():
        if tag == "equal":
            result.extend(a[i:j])
        else:
            # Keep a partial replacement at its original offset within the block.
            # Appending all additions after retained deletions would move selected
            # replacements past neighbouring lines that were not selected.
            for offset in range(max(j - i, l - k)):
                if i + offset < j and f"d:{i + offset}" not in selected:
                    result.append(a[i + offset])
                if k + offset < l and f"a:{k + offset}" in selected:
                    result.append(b[k + offset])
    data = b"".join(result)
    if not new.exists and selected == valid and not data:
        return FileVersion.of(None)
    return FileVersion.of(data, old.mode if old.exists else new.mode)


def diff_fragments(snapshot: Snapshot) -> list[dict]:
    result = []
    for file in snapshot.files:
        if not file.dirty:
            continue
        for layer, old, new in (("staged", file.head, file.index), ("unstaged", file.index, file.work)):
            if old.fingerprint() == new.fingerprint():
                continue
            rows = [] if file.unsupported else line_changes(old, new)
            # Display slices retain canonical line IDs; grouping never changes operations.
            chunks = [rows[n:n + 40] for n in range(0, len(rows), 40)] or [[]]
            for number, chunk in enumerate(chunks):
                fid = digest(f"{file.path}\0{layer}\0{old.fingerprint()}\0{new.fingerprint()}\0{number}".encode())[:24]
                result.append(dict(id=fid, path=file.path, layer=layer, part=number + 1,
                                   parts=len(chunks), rows=chunk, unsupported=file.unsupported,
                                   old_exists=old.exists, new_exists=new.exists,
                                   old_no_newline=bool(old.bytes()) and not old.bytes().endswith(b"\n"),
                                   new_no_newline=bool(new.bytes()) and not new.bytes().endswith(b"\n")))
    return result


class GitRepo:
    def __init__(self, config: Config):
        self.config = config
        self.root = config.repo
        self.lock = asyncio.Lock()
        self.selected_untracked: set[str] = set()

    async def run(self, *args: str, data: bytes | None = None, env: dict | None = None,
                  check: bool = True) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            "git", "-c", "core.fsmonitor=false", "-C", str(self.root), *args,
            stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", **(env or {})},
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(data), 30)
        except (TimeoutError, asyncio.CancelledError):
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
        if check and proc.returncode:
            raise ReviewError(err.decode("utf-8", "replace").strip() or "Git command failed", 409)
        return out

    async def initialize(self):
        root = (await self.run("rev-parse", "--show-toplevel")).decode().strip()
        self.root = Path(root).resolve()
        index = (await self.run("rev-parse", "--git-path", "index")).decode().strip()
        self.index_path = (self.root / index).resolve()

    def safe_path(self, path: str) -> Path:
        p = Path(path)
        if not path or p.is_absolute() or ".." in p.parts or ".git" in p.parts or "\x00" in path:
            raise ReviewError("Invalid repository path")
        target = self.root / p
        for parent in target.parents:
            if parent == self.root:
                break
            if parent.is_symlink():
                raise ReviewError("Symlink parents are not supported")
        return target

    def _read_work(self, path: str) -> FileVersion:
        target = self.safe_path(path)
        try:
            st = target.lstat()
        except FileNotFoundError:
            return FileVersion.of(None)
        if stat.S_ISLNK(st.st_mode):
            return FileVersion.of(os.fsencode(os.readlink(target)), "120000")
        if not stat.S_ISREG(st.st_mode):
            return FileVersion.of(b"", "160000")
        mode = "100755" if st.st_mode & stat.S_IXUSR else "100644"
        if st.st_size > self.config.max_file_bytes:
            import hashlib
            h = hashlib.sha256()
            with target.open("rb") as f:
                while block := f.read(1024 * 1024):
                    h.update(block)
            return FileVersion(exists=True, mode=mode, omitted_hash=h.hexdigest())
        version = FileVersion.of(target.read_bytes(), mode)
        version.permissions = stat.S_IMODE(st.st_mode)
        return version

    async def _blob(self, entry: tuple[str, str] | None) -> FileVersion:
        if not entry:
            return FileVersion.of(None)
        mode, oid = entry
        if mode == "160000":
            return FileVersion.of(oid.encode(), mode)
        size = int(await self.run("cat-file", "-s", oid))
        if size > self.config.max_file_bytes:
            return FileVersion(exists=True, mode=mode, omitted_hash=oid)
        return FileVersion.of(await self.run("cat-file", "blob", oid), mode)

    async def index_bytes(self) -> bytes | None:
        def read():
            return self.index_path.read_bytes() if self.index_path.exists() else None
        return await asyncio.to_thread(read)

    async def index_version(self, env=None) -> str:
        # Stat-cache refreshes are not semantic edits to the staged tree.
        return digest(await self.run("ls-files", "--stage", "-v", "-z", env=env))

    async def capture(self, extra_paths: set[str] | None = None, _attempt: int = 0) -> Snapshot:
        head = (await self.run("rev-parse", "--verify", "HEAD", check=False)).decode().strip() or None
        index_raw = await self.index_bytes()
        index_version = await self.index_version()
        entries: dict[str, tuple[str, str]] = {}
        conflicts = False
        for record in (await self.run("ls-files", "--stage", "-z")).split(b"\0"):
            if not record:
                continue
            info, path = record.split(b"\t", 1)
            mode, oid, stage = info.decode().split()
            entries[os.fsdecode(path)] = (mode, oid)
            conflicts |= stage != "0"
        head_entries = {}
        if head:
            for record in (await self.run("ls-tree", "-r", "-z", head)).split(b"\0"):
                if record:
                    info, path = record.split(b"\t", 1)
                    mode, _, oid = info.decode().split()
                    head_entries[os.fsdecode(path)] = (mode, oid)
        staged = set(path for path in entries.keys() | head_entries.keys()
                     if entries.get(path) != head_entries.get(path))
        work_changed = {os.fsdecode(p) for p in (await self.run("diff", "--no-ext-diff", "--no-renames", "--name-only", "-z")).split(b"\0") if p}
        untracked = {os.fsdecode(p) for p in (await self.run("ls-files", "--others", "--exclude-standard", "-z")).split(b"\0") if p}
        dirty_paths = staged | work_changed | (untracked & self.selected_untracked)
        paths = dirty_paths | (extra_paths or set())
        files = []
        autocrlf = (await self.run("config", "--get", "core.autocrlf", check=False)).strip() not in (b"", b"false")
        for path in sorted(paths):
            h, i = await self._blob(head_entries.get(path)), await self._blob(entries.get(path))
            try:
                w = await asyncio.to_thread(self._read_work, path)
                reason = None
            except ReviewError as exc:
                w, reason = FileVersion.of(b"", "120000"), str(exc)
            versions = (h, i, w)
            if any(v.exists and v.mode not in ("100644", "100755") for v in versions):
                reason = "Symlinks and submodules are view-only"
            if any(v.omitted_hash for v in versions):
                reason = "File exceeds the configured size limit; content omitted"
            if any(v.exists and (b"\0" in v.bytes()) for v in versions):
                reason = "Binary files are view-only"
            try:
                for v in versions:
                    v.bytes().decode("utf-8")
            except UnicodeDecodeError:
                reason = "Non-UTF-8 files are view-only"
            if not reason and len({v.mode for v in versions if v.exists}) > 1:
                reason = "File mode changes are view-only"
            attrs = (await self.run("check-attr", "-z", "filter", "working-tree-encoding", "text", "eol", "--", path)).split(b"\0")
            if autocrlf or any(value not in (b"unspecified", b"unset") for value in attrs[2::3]):
                reason = "Files with Git content conversions are view-only"
            if conflicts:
                reason = "Resolve the conflicted index before changing files"
            files.append(FileState(path=path, head=h, index=i, work=w, unsupported=reason, dirty=path in dirty_paths))
        version = self.version(head, index_version, files)
        final_head = (await self.run("rev-parse", "--verify", "HEAD", check=False)).decode().strip() or None
        if index_version != await self.index_version() or head != final_head:
            if _attempt < 2:
                return await self.capture(extra_paths, _attempt + 1)
            raise ReviewError("Index changed while taking a snapshot; retry", 409)
        index_raw = await self.index_bytes()
        return Snapshot(version=version, head=head, index_version=index_version, index_content=base64.b64encode(index_raw or b"").decode(),
                        files=files, omitted_untracked=sorted(untracked - self.selected_untracked))

    @staticmethod
    def version(head: str | None, index_version: str, files: list[FileState]) -> str:
        working = [(f.path, f.work.fingerprint(), f.work.permissions) for f in files if f.dirty]
        return digest(json.dumps([head, index_version, sorted(working)]).encode())

    def prepare_content(self, snapshot: Snapshot, req: OperationRequest) -> tuple[FileState, FileVersion]:
        file = next((f for f in snapshot.files if f.path == req.path), None)
        if file is None or file.unsupported:
            raise ReviewError(file.unsupported if file else "File is not part of this snapshot")
        if req.action == "stage":
            target = select_changes(file.index, file.work, req.line_ids, req.whole_file)
        else:
            old, new = (file.head, file.index) if req.action == "unstage" else (file.index, file.work)
            if req.whole_file:
                target = old.model_copy(deep=True)
            else:
                # Undo selected diff operations by retaining their complement.
                all_ids = {r["id"] for r in line_changes(old, new) if r["id"]}
                if not req.line_ids or not set(req.line_ids) <= all_ids:
                    raise ReviewError("Select changed lines from the displayed diff")
                remaining = list(all_ids - set(req.line_ids))
                target = select_changes(old, new, remaining, False) if remaining else old.model_copy(deep=True)
        return file, target

    async def build_index(self, snapshot: Snapshot, path: str, target: FileVersion) -> tuple[bytes, str]:
        with tempfile.TemporaryDirectory(dir=self.config.state_dir) as tmp:
            index_path = Path(tmp) / "index"
            if snapshot.index_content:
                await asyncio.to_thread(index_path.write_bytes, base64.b64decode(snapshot.index_content))
            env = {"GIT_INDEX_FILE": str(index_path)}
            if target.exists:
                oid = (await self.run("hash-object", "-w", "--stdin", data=target.bytes())).strip()
                line = target.mode.encode() + b" " + oid + b"\t" + os.fsencode(path) + b"\0"
            else:
                # SHA length follows the repository's object format.
                fmt = (await self.run("rev-parse", "--show-object-format")).strip()
                line = b"0 " + b"0" * (64 if fmt == b"sha256" else 40) + b"\t" + os.fsencode(path) + b"\0"
            await self.run("update-index", "-z", "--index-info", data=line, env=env)
            return await asyncio.to_thread(index_path.read_bytes), await self.index_version(env)

    def _write_index(self, expected: bytes, desired: bytes):
        lock = self.index_path.with_name(self.index_path.name + ".lock")
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise ReviewError("Git index is locked by another process", 409)
        try:
            actual = self.index_path.read_bytes() if self.index_path.exists() else b""
            if actual != expected:
                raise ReviewError("Git index changed; refresh the diff", 409)
            with os.fdopen(fd, "wb") as f:
                fd = -1
                f.write(desired)
                f.flush()
                os.fsync(f.fileno())
            if desired:
                os.replace(lock, self.index_path)
            else:
                self.index_path.unlink(missing_ok=True)
        finally:
            if fd != -1:
                os.close(fd)
            lock.unlink(missing_ok=True)

    def _write_work(self, path: str, expected: FileVersion, desired: FileVersion):
        target = self.safe_path(path)
        if self._read_work(path).fingerprint() != expected.fingerprint():
            raise ReviewError("Working file changed; refresh the diff", 409)
        if not desired.exists:
            target.unlink(missing_ok=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".review-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(desired.bytes())
                f.flush()
                os.fsync(f.fileno())
            permissions = desired.permissions if desired.permissions is not None else expected.permissions
            os.chmod(temporary, permissions if permissions is not None else (0o755 if desired.mode == "100755" else 0o644))
            if self._read_work(path).fingerprint() != expected.fingerprint():
                raise ReviewError("Working file changed; refresh the diff", 409)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)

    async def write(self, snapshot: Snapshot, file: FileState, target: FileVersion,
                    index: bytes | None):
        current = await self.capture({file.path})
        if current.version != snapshot.version:
            raise ReviewError("Repository changed; refresh the diff", 409)
        if index is not None:
            await asyncio.to_thread(self._write_index, base64.b64decode(current.index_content), index)
        else:
            await asyncio.to_thread(self._write_work, file.path, file.work, target)
