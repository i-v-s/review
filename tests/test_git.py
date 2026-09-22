import base64

import pytest

from review_service.git import diff_fragments
from review_service.models import OperationRequest, ReviewError, uid
from conftest import git_command


async def operate(service, action, lines=None, whole=False, path="example.py"):
    snap = await service.refresh()
    req = OperationRequest(snapshot_id=snap.id, expected_version=snap.version, path=path,
                           action=action, line_ids=lines or [], whole_file=whole, key=uid())
    return await service.operation(req)


async def test_partial_stage_keeps_existing_index(service, repo):
    file = repo / "example.py"
    file.write_text("ONE\ntwo\nthree\nfour\n")
    git_command(repo, "add", "example.py")
    file.write_text("ONE\nTWO\nTHREE\nfour\n")
    await operate(service, "stage", ["d:1", "a:1"])
    assert git_command(repo, "show", ":example.py") == b"ONE\nTWO\nthree\nfour\n"
    assert file.read_bytes() == b"ONE\nTWO\nTHREE\nfour\n"


async def test_unstage_changes_only_selected_lines(service, repo):
    file = repo / "example.py"
    file.write_text("ONE\nTWO\nthree\nfour\n")
    git_command(repo, "add", "example.py")
    await operate(service, "unstage", ["d:0", "a:0"])
    assert git_command(repo, "show", ":example.py") == b"one\nTWO\nthree\nfour\n"
    assert file.read_bytes() == b"ONE\nTWO\nthree\nfour\n"


async def test_discard_and_durable_undo(service, repo):
    file = repo / "example.py"
    file.write_text("ONE\nTWO\nthree\nfour\n")
    op = await operate(service, "discard", ["d:0", "a:0"])
    assert file.read_bytes() == b"one\nTWO\nthree\nfour\n"
    undo = await service.undo(op["id"], uid())
    assert undo["status"] == "completed"
    assert file.read_bytes() == b"ONE\nTWO\nthree\nfour\n"
    assert (await service.store.get("operation", op["id"]))["status"] == "completed"


async def test_stage_undo_preserves_work(service, repo):
    file = repo / "example.py"
    file.write_text("changed\n")
    op = await operate(service, "stage", whole=True)
    await service.undo(op["id"], uid())
    assert git_command(repo, "show", ":example.py") == b"one\ntwo\nthree\nfour\n"
    assert file.read_bytes() == b"changed\n"


async def test_stale_snapshot_rejected(service, repo):
    file = repo / "example.py"
    file.write_text("first\n")
    snapshot = await service.refresh()
    file.write_text("second\n")
    req = OperationRequest(snapshot_id=snapshot.id, expected_version=snapshot.version,
                           path="example.py", action="discard", whole_file=True, key=uid())
    with pytest.raises(ReviewError, match="changed"):
        await service.operation(req)
    assert file.read_bytes() == b"second\n"


async def test_undo_refuses_newer_work(service, repo):
    file = repo / "example.py"
    file.write_text("first\n")
    op = await operate(service, "discard", whole=True)
    file.write_text("later\n")
    with pytest.raises(ReviewError, match="overwrite"):
        await service.undo(op["id"], uid())
    assert file.read_bytes() == b"later\n"


async def test_idempotence_and_key_reuse(service, repo):
    (repo / "example.py").write_text("first\n")
    s = await service.refresh()
    req = OperationRequest(snapshot_id=s.id, expected_version=s.version, path="example.py",
                           action="stage", whole_file=True, key=uid())
    op = await service.operation(req)
    assert await service.operation(req) == op
    with pytest.raises(ReviewError, match="Idempotency"):
        await service.operation(req.model_copy(update={"action": "discard"}))


@pytest.mark.parametrize("data", ["Привет\r\nмир\r\n".encode(), b"no final newline", b"same\nsame\nx\nsame\n"])
async def test_exact_bytes(service, repo, data):
    (repo / "example.py").write_bytes(data)
    await operate(service, "stage", whole=True)
    assert git_command(repo, "show", ":example.py") == data


async def test_new_and_deleted_files(service, repo):
    new = repo / "new file.txt"
    new.write_text("a\nb\n")
    service.git.selected_untracked.add(new.name)
    await operate(service, "stage", ["a:1"], path=new.name)
    assert git_command(repo, "show", ":new file.txt") == b"b\n"
    new.unlink()
    op = await operate(service, "stage", whole=True, path=new.name)
    assert "new file.txt" not in git_command(repo, "ls-files").decode()
    await service.undo(op["id"], uid())
    assert git_command(repo, "show", ":new file.txt") == b"b\n"


async def test_binary_and_symlink_are_read_only(service, repo):
    (repo / "example.py").write_bytes(b"\0binary")
    with pytest.raises(ReviewError, match="Binary"):
        await operate(service, "stage", whole=True)
    (repo / "example.py").unlink()
    (repo / "example.py").symlink_to("/etc/passwd")
    with pytest.raises(ReviewError, match="Symlink"):
        await operate(service, "discard", whole=True)


async def test_fragment_limit_and_coverage(service, repo):
    (repo / "example.py").write_text("".join(f"{n}\n" for n in range(130)))
    fragments = diff_fragments(await service.refresh())
    assert all(len(f["rows"]) <= 50 for f in fragments)
    assert sum(r["kind"] == "add" for f in fragments for r in f["rows"]) == 130


async def test_empty_new_file(service, repo):
    (repo / "empty").touch()
    service.git.selected_untracked.add("empty")
    await operate(service, "stage", whole=True, path="empty")
    assert git_command(repo, "show", ":empty") == b""


async def test_discard_and_undo_preserve_private_permissions(service, repo):
    file = repo / 'example.py'
    file.chmod(0o600)
    file.write_text('private content\n')
    op = await operate(service, 'discard', whole=True)
    assert file.stat().st_mode & 0o777 == 0o600
    await service.undo(op['id'], uid())
    assert file.stat().st_mode & 0o777 == 0o600
    assert file.read_text() == 'private content\n'
