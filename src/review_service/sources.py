from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp

from .models import ReviewError, SourceEvent, digest


def normalize(provider: str, session_id: str, messages: list[dict]) -> list[SourceEvent]:
    events = []
    for number, message in enumerate(messages):
        info = message.get("info", message)
        source_id = str(info.get("id") or message.get("id") or digest(json.dumps(message, sort_keys=True).encode())[:24])
        role = info.get("role") or info.get("type", "unknown")
        parts = message.get("parts", message.get("content", []))
        if isinstance(parts, str):
            text = parts
        elif isinstance(parts, list):
            text = "\n".join(p.get("text", json.dumps(p, ensure_ascii=False)) if isinstance(p, dict) else str(p) for p in parts)
        else:
            text = json.dumps(parts, ensure_ascii=False)
        if not text:
            text = str(info.get("text") or json.dumps(message, ensure_ascii=False))
        time = info.get("time")
        timestamp = info.get("createdAt") or (time.get("created") if isinstance(time, dict) else time)
        events.append(SourceEvent(
            id=f"{provider}:{session_id}:{source_id}", provider=provider, session_id=session_id,
            source_id=source_id, role=str(role), text=text,
            timestamp=str(timestamp) if timestamp is not None else None,
            raw=message,
        ))
    return events


class CodexAdapter:
    def __init__(self, command: str, repo: Path):
        self.command, self.repo = command, repo
        self.process = None
        self.reader = None
        self.stderr_reader = None
        self.connect_lock = asyncio.Lock()
        self.pending = {}
        self.counter = 0

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                future = self.pending.get(message.get("id"))
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(ReviewError(f"Codex: {message['error'].get('message', 'RPC error')}", 502))
                    else:
                        future.set_result(message.get("result", {}))
        except (ValueError, OSError) as exc:
            error = str(exc)
        else:
            error = "connection closed"
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ReviewError(f"Codex: {error}", 502))

    async def _drain_stderr(self):
        while await self.process.stderr.read(4096):
            pass

    async def start(self):
        async with self.connect_lock:
            if self.process and self.process.returncode is None and self.reader and not self.reader.done():
                return
            await self.close()
            try:
                self.process = await asyncio.create_subprocess_exec(
                    self.command, "app-server", "--listen", "stdio://", cwd=self.repo,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024,
                )
                self.reader = asyncio.create_task(self._read())
                self.stderr_reader = asyncio.create_task(self._drain_stderr())
                await self.request("initialize", {"clientInfo": {"name": "review_service", "version": "0.1.0"}})
                self.process.stdin.write(b'{"method":"initialized","params":{}}\n')
                await self.process.stdin.drain()
            except (OSError, TimeoutError) as exc:
                await self.close()
                raise ReviewError(f"Codex unavailable: {exc}", 503) from exc

    async def request(self, method: str, params: dict):
        self.counter += 1
        id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[id] = future
        try:
            self.process.stdin.write((json.dumps({"id": id, "method": method, "params": params}) + "\n").encode())
            await self.process.stdin.drain()
            return await asyncio.wait_for(future, 20)
        except (BrokenPipeError, TimeoutError) as exc:
            raise ReviewError(f"Codex request failed: {method}", 503) from exc
        finally:
            self.pending.pop(id, None)

    async def sessions(self):
        await self.start()
        cursor, sessions = None, []
        while True:
            params = {"cwd": str(self.repo), "limit": 100}
            if cursor:
                params["cursor"] = cursor
            result = await self.request("thread/list", params)
            sessions.extend({"provider": "codex", "id": t["id"], "title": t.get("name") or t.get("preview") or t["id"]} for t in result.get("data", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return sessions

    async def events(self, session_id: str):
        await self.start()
        result = await self.request("thread/read", {"threadId": session_id, "includeTurns": True})
        thread = result.get("thread", {})
        cwd = thread.get("cwd")
        if cwd and Path(cwd).resolve() != self.repo:
            raise ReviewError("Codex session belongs to another repository")
        items = [item for turn in thread.get("turns", []) for item in turn.get("items", [])]
        return normalize("codex", session_id, items)

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in (self.reader, self.stderr_reader):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.process = None


class OpenCodeAdapter:
    def __init__(self, http: aiohttp.ClientSession, url: str, repo: Path,
                 username: str = "opencode", password: str = ""):
        self.http, self.url, self.repo = http, url.rstrip("/"), repo
        self.auth = aiohttp.BasicAuth(username, password) if password else None

    async def get(self, path: str):
        if not self.url:
            raise ReviewError("Set OPENCODE_URL to connect to OpenCode", 503)
        try:
            async with self.http.get(self.url + path, params={"directory": str(self.repo)}, auth=self.auth) as response:
                response.raise_for_status()
                return await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise ReviewError(f"OpenCode unavailable: {type(exc).__name__}", 503) from exc

    async def sessions(self):
        result = await self.get("/session")
        return [dict(provider="opencode", id=s["id"], title=s.get("title", s["id"])) for s in result
                if not s.get("directory") or Path(s["directory"]).resolve() == self.repo]

    async def events(self, session_id: str):
        from urllib.parse import quote
        session_id = quote(session_id, safe="")
        info = await self.get(f"/session/{session_id}")
        if info.get("directory") and Path(info["directory"]).resolve() != self.repo:
            raise ReviewError("OpenCode session belongs to another repository")
        return normalize("opencode", info["id"], await self.get(f"/session/{session_id}/message"))


def import_opencode(body: dict) -> tuple[dict, list[SourceEvent]]:
    info = body.get("info") or body.get("session")
    if not isinstance(info, dict) or not info.get("id") or not isinstance(body.get("messages"), list):
        raise ReviewError("Expected an OpenCode export with info and messages")
    return dict(provider="import", id=info["id"], title=info.get("title", info["id"])), normalize("opencode-import", info["id"], body["messages"])
