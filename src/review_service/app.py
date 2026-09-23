from __future__ import annotations

import asyncio
import fcntl
import json
import os
import secrets
from contextlib import AsyncExitStack
from pathlib import Path

import aiohttp
from aiohttp import web
from pydantic import ValidationError

from .config import Config
from .git import GitRepo
from .llm import LLM
from .models import FileEditRequest, OperationRequest, ReviewError, SourceEvent, now, uid
from .service import Service
from .sources import CodexAdapter, OpenCodeAdapter
from .storage import Store


SERVICE = web.AppKey("service", Service)
CONFIG = web.AppKey("config", Config)
STATIC = Path(__file__).parent / "static"


@web.middleware
async def errors(request, handler):
    try:
        response = await handler(request)
    except ReviewError as exc:
        response = web.json_response({"error": str(exc)}, status=exc.status)
    except (ValidationError, ValueError, KeyError, TypeError) as exc:
        response = web.json_response({"error": f"Invalid request: {str(exc)[:500]}"}, status=400)
    response.headers.update({
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'" +
                                   (f" 'nonce-{request['csp_nonce']}'" if 'csp_nonce' in request else "") +
                                   "; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        "Cache-Control": "no-store",
    })
    return response


@web.middleware
async def auth(request, handler):
    if request.path.startswith("/api/"):
        supplied = request.headers.get("Authorization", "")
        expected = "Bearer " + (request.app[CONFIG].token or "")
        if not secrets.compare_digest(supplied, expected):
            raise ReviewError("Authentication required", 401)
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            raise ReviewError("Origin is not allowed", 403)
    return await handler(request)


async def lifecycle(app):
    config = app[CONFIG]
    await asyncio.to_thread(config.state_dir.mkdir, parents=True, exist_ok=True, mode=0o700)
    if not config.token:
        token_path = config.state_dir / "token"
        if not token_path.exists():
            fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32))
        config.token = token_path.read_text().strip()
    git = GitRepo(config)
    await git.initialize()
    async with AsyncExitStack() as stack:
        lock = stack.enter_context(open(git.index_path.parent / "review-service.lock", "a"))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another review service is already using this repository")
        store = Store(config.state_dir / "review.sqlite3")
        await store.open()
        stack.push_async_callback(store.close)
        llm = LLM(config)
        stack.push_async_callback(llm.close)
        await llm.open()
        http = await stack.enter_async_context(aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)))
        codex = CodexAdapter(config.codex_command, git.root)
        stack.push_async_callback(codex.close)
        service = Service(config, store, git, llm, {
            "codex": codex,
            "opencode": OpenCodeAdapter(http, config.opencode_url, git.root, config.opencode_username, config.opencode_password),
        })
        app[SERVICE] = service
        poll = None
        try:
            await service.initialize()
            poll = asyncio.create_task(service.poll())
            yield
        finally:
            service.stopping = True
            tasks = list(service.tasks.values()) + ([poll] if poll else [])
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def state(request):
    return web.json_response(await request.app[SERVICE].state())


async def models(request):
    return web.json_response({"models": await request.app[SERVICE].llm.models()})


async def select_model(request):
    body = await request.json()
    if not isinstance(body, dict) or set(body) != {"model"}:
        raise ReviewError("Ожидается поле model: ID модели или null для значения из окружения.")
    return web.json_response(await request.app[SERVICE].select_model(body["model"]))


async def sessions(request):
    return web.json_response(await request.app[SERVICE].sessions())


async def select_sessions(request):
    service = request.app[SERVICE]
    body = await request.json()
    selected = body.get("sessions", [])
    if not isinstance(selected, list) or len(selected) > 30:
        raise ReviewError("Select at most 30 sessions")
    for s in selected:
        if s.get("provider") not in ("codex", "opencode", "import") or not isinstance(s.get("id"), str):
            raise ReviewError("Invalid session selection")
    await service.store.set_setting("sessions", selected)
    await service.sync()
    return web.json_response({"ok": True})


async def imported(request):
    return web.json_response(await request.app[SERVICE].import_history(await request.json()))


async def sync(request):
    service = request.app[SERVICE]
    body = await request.json() if request.content_length else {}
    if not isinstance(body, dict):
        raise ReviewError("Expected a JSON object")
    paths = body.get("paths", [])
    if not isinstance(paths, list) or len(paths) > 10 or not all(isinstance(path, str) for path in paths):
        raise ReviewError("paths must contain at most 10 repository paths")
    for path in paths:
        service.git.safe_path(path)
    async with service.git.lock:
        await service.refresh(set(paths))
    await service.sync()
    return web.json_response(await service.state())


async def review_start(request):
    service = request.app[SERVICE]
    body = await request.json()
    async with service.git.lock:
        snapshot = await service.refresh()
        await service.store.set_setting("baseline", snapshot.id)
        await service.store.set_setting("retrospective", not bool(body.get("from_start", False)))
    return web.json_response({"baseline_id": snapshot.id})


async def untracked(request):
    service = request.app[SERVICE]
    body = await request.json()
    paths = body.get("paths", [])
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ReviewError("paths must be an array of repository paths")
    for path in paths:
        service.git.safe_path(path)
    async with service.git.lock:
        service.git.selected_untracked.update(paths)
        await service.store.set_setting("untracked", sorted(service.git.selected_untracked))
        await service.refresh()
    return web.json_response({"ok": True})


async def snapshot(request):
    s = await request.app[SERVICE].store.snapshot(request.match_info["id"])
    if not s:
        raise ReviewError("Snapshot not found", 404)
    return web.json_response(await Service.public_snapshot(s))


async def file_content(request):
    s = await request.app[SERVICE].store.snapshot(request.match_info["id"])
    if not s:
        raise ReviewError("Snapshot not found", 404)
    file = next((f for f in s.files if f.path == request.query.get("path")), None)
    side = request.query.get("side", "work")
    if not file or side not in ("head", "index", "work"):
        raise ReviewError("File version not found", 404)
    v = getattr(file, side)
    return web.json_response(dict(content=v.bytes().decode("utf-8", "replace"), exists=v.exists,
                                  omitted=bool(v.omitted_hash), unsupported=file.unsupported))


async def report_get(request):
    report = await request.app[SERVICE].store.get("report", request.match_info["id"])
    if not report:
        raise ReviewError("Report not found", 404)
    return web.json_response(report)


async def draft_get(request):
    draft = await request.app[SERVICE].store.get("report_draft", request.match_info["id"])
    if not draft:
        raise ReviewError("Report draft not found", 404)
    return web.json_response(draft)


async def report_generate(request):
    service = request.app[SERVICE]
    if not service.llm.available:
        raise ReviewError("Настройте OPENAI_API_KEY и выберите модель в интерфейсе или REVIEW_MODEL.", 503)
    model = service.llm.model
    return web.json_response(await service.start_job("report", lambda id: service.generate(id, model=model)), status=202)


async def review_mark(request):
    service = request.app[SERVICE]
    body = await request.json()
    report = await service.store.get("report", request.match_info["id"])
    item = next((i for i in report["items"] if i["id"] == request.match_info["item"]), None) if report else None
    if not item:
        raise ReviewError("Review item not found", 404)
    if not isinstance(body.get("reviewed"), bool):
        raise ReviewError("reviewed must be boolean")
    item["reviewed"] = body["reviewed"]
    await service.store.put("report", report["id"], report)
    return web.json_response(item)


async def finding_mark(request):
    service = request.app[SERVICE]
    body = await request.json()
    report = await service.store.get("report", request.match_info["id"])
    finding = next((f for f in report.get("findings", []) if f["id"] == request.match_info["finding"]), None) if report else None
    if not finding:
        raise ReviewError("Finding not found", 404)
    if not isinstance(body.get("reviewed"), bool):
        raise ReviewError("reviewed must be boolean")
    finding["reviewed"] = body["reviewed"]
    await service.store.put("report", report["id"], report)
    return web.json_response(finding)


async def sources(request):
    service = request.app[SERVICE]
    if request.query.get("draft_id"):
        evidence = await service.store.get("report_draft_evidence", request.query["draft_id"])
        if not evidence:
            raise ReviewError("Report draft evidence not found", 404)
        return web.json_response(evidence)
    if request.query.get("report_id"):
        evidence = await service.store.get("report_evidence", request.query["report_id"])
        if not evidence:
            raise ReviewError("Report evidence not found", 404)
        return web.json_response(evidence)
    events = await service.store.list("event") if request.query.get("all") == "1" else await service.events()
    return web.json_response({"events": events})


async def decision(request):
    body = await request.json()
    text = body.get("text", "")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 20000:
        raise ReviewError("Decision text must contain 1–20000 characters")
    event = SourceEvent(id="decision:" + uid(), provider="decision", session_id=str(body.get("session_id", "manual")),
                        source_id=uid(), role="decision", text=text, timestamp=now())
    await request.app[SERVICE].store.event(event)
    return web.json_response(event.model_dump(), status=201)


async def operation(request):
    req = OperationRequest.model_validate(await request.json())
    return web.json_response(await request.app[SERVICE].operation(req, preview=request.path.endswith("/preview")))


async def edit_file(request):
    req = FileEditRequest.model_validate(await request.json())
    return web.json_response(await request.app[SERVICE].edit_file(req))


async def undo(request):
    body = await request.json()
    key = body.get("key")
    if not isinstance(key, str) or not 8 <= len(key) <= 128:
        raise ReviewError("An idempotency key is required")
    return web.json_response(await request.app[SERVICE].undo(request.match_info["id"], key))


async def chat(request):
    service = request.app[SERVICE]
    body = await request.json()
    question = body.get("question", "")
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 10000:
        raise ReviewError("Question must contain 1–10000 characters")
    if not service.llm.available:
        raise ReviewError("Настройте OPENAI_API_KEY и выберите модель в интерфейсе или REVIEW_MODEL.", 503)
    model = service.llm.model
    return web.json_response(await service.start_job("chat", lambda id: service.chat(
        id, question, body["snapshot_id"], body.get("report_id"), body.get("item_id"), model=model,
        target_kind=body.get("target_kind"), target_id=body.get("target_id"),
        selections=body.get("selections"))), status=202)


async def messages(request):
    data = await request.app[SERVICE].store.list("message")
    thread = request.query.get("thread")
    return web.json_response({"messages": [m for m in data if not thread or m["thread"] == thread]})


async def job_cancel(request):
    service = request.app[SERVICE]
    task = service.tasks.get(request.match_info["id"])
    if task:
        task.cancel()
    return web.json_response({"ok": True})


async def events(request):
    service = request.app[SERVICE]
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    await response.prepare(request)
    queue = asyncio.Queue(maxsize=128)
    service.subscribers.add(queue)
    try:
        await response.write(b'data: {"type":"connected"}\n\n')
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), 15)
            except TimeoutError:
                await response.write(b": keepalive\n\n")
                continue
            if event["type"] == "shutdown":
                break
            await response.write(("data: " + json.dumps(event, ensure_ascii=True) + "\n\n").encode())
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        service.subscribers.discard(queue)
    return response


async def index(request):
    nonce = secrets.token_urlsafe(18)
    request["csp_nonce"] = nonce
    html = (STATIC / "index.html").read_text().replace('content="CSP_NONCE"', f'content="{nonce}"')
    return web.Response(text=html, content_type="text/html")


async def shutdown(app):
    service = app.get(SERVICE)
    if service:
        for queue in service.subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait({"type": "shutdown"})


def create_app(config: Config) -> web.Application:
    app = web.Application(middlewares=[errors, auth], client_max_size=16 * 1024 * 1024)
    app[CONFIG] = config
    app.cleanup_ctx.append(lifecycle)
    app.on_shutdown.append(shutdown)
    app.add_routes([
        web.get("/", index), web.static("/static", STATIC),
        web.get("/api/v1/state", state), web.get("/api/v1/sessions", sessions),
        web.get("/api/v1/models", models), web.put("/api/v1/model", select_model),
        web.put("/api/v1/sessions", select_sessions), web.post("/api/v1/import", imported),
        web.post("/api/v1/sync", sync), web.post("/api/v1/reviews", review_start),
        web.post("/api/v1/untracked", untracked),
        web.get("/api/v1/snapshots/{id}", snapshot), web.get("/api/v1/snapshots/{id}/file", file_content),
        web.get("/api/v1/reports/{id}", report_get), web.post("/api/v1/reports", report_generate),
        web.get("/api/v1/report-drafts/{id}", draft_get),
        web.patch("/api/v1/reports/{id}/items/{item}", review_mark),
        web.patch("/api/v1/reports/{id}/findings/{finding}", finding_mark),
        web.get("/api/v1/sources", sources), web.post("/api/v1/decisions", decision),
        web.post("/api/v1/operations/preview", operation), web.post("/api/v1/operations", operation),
        web.post("/api/v1/operations/edit", edit_file),
        web.post("/api/v1/operations/{id}/undo", undo),
        web.post("/api/v1/questions", chat), web.get("/api/v1/messages", messages),
        web.post("/api/v1/jobs/{id}/cancel", job_cancel), web.get("/api/v1/events", events),
    ])
    return app
