from __future__ import annotations

import asyncio
import base64
import json
from collections import defaultdict

from .git import GitRepo, diff_fragments
from .llm import LLM
from .models import OperationRequest, ReviewError, ReviewItem, SourceEvent, digest, now, uid
from .sources import import_opencode
from .storage import Store


class Service:
    def __init__(self, config, store: Store, git: GitRepo, llm: LLM, adapters):
        self.config, self.store, self.git, self.llm, self.adapters = config, store, git, llm, adapters
        self.current = None
        self.subscribers = set()
        self.tasks = {}
        self.source_status = {}
        self.sync_lock = asyncio.Lock()
        self.active_report = None
        self.stopping = False

    async def emit(self, type: str, **payload):
        event = dict(type=type, **payload)
        for queue in list(self.subscribers):
            if queue.full():
                queue.get_nowait()
                queue.put_nowait({"type": "resync"})
            else:
                queue.put_nowait(event)

    async def refresh(self, extra_paths=None):
        snapshot = await self.git.capture(extra_paths)
        if self.current is None or snapshot.version != self.current.version:
            await self.store.save_snapshot(snapshot)
            self.current = snapshot
            await self.emit("snapshot", id=snapshot.id, version=snapshot.version)
        elif extra_paths:
            # Keep operation-only file entries without changing the public snapshot identity.
            snapshot.id = self.current.id
            self.current = snapshot
            await self.store.save_snapshot(snapshot)
        return self.current

    async def initialize(self):
        self.git.selected_untracked = set(await self.store.setting("untracked", []))
        await self.refresh()
        if not await self.store.setting("baseline"):
            await self.store.set_setting("baseline", self.current.id)
        for job in await self.store.list("job"):
            if job["status"] in ("running", "queued"):
                job.update(status="interrupted", error="Server restarted")
                await self.store.put("job", job["id"], job)
        for draft in await self.store.list("report_draft"):
            if draft["status"] == "running":
                report = await self.store.get("report", draft["report_id"])
                draft.update(status="completed" if report else "interrupted",
                             error="" if report else "Сервис перезапущен. Генерация не завершена.")
                await self.save_draft(draft)
                if report:
                    job = await self.store.get("job", draft["id"])
                    if job:
                        job.update(status="completed", result={"report_id": report["id"]})
                        job.pop("error", None)
                        await self.store.put("job", job["id"], job)
        for op in await self.store.list("operation"):
            if op["status"] == "prepared":
                await self.recover(op)

    async def recover(self, op):
        current = await self.git.capture({op["path"]})
        file = next(f for f in current.files if f.path == op["path"])
        if (current.index_version == op["desired_index_hash"]
                and file.work.fingerprint() == op["desired_work_hash"] and current.head == op["head"]):
            await self.store.save_snapshot(current)
            self.current = current
            op.update(status="completed", after_id=current.id, after_version=current.version, recovered=True)
        elif current.version == op["before_version"]:
            op.update(status="failed", error="Interrupted before applying changes")
        else:
            op.update(status="interrupted", error="State changed after interruption; backups retained for inspection")
        await self.store.put("operation", op["id"], op)

    async def state(self):
        reports = await self.store.list("report")
        baseline = await self.store.snapshot(await self.store.setting("baseline"))
        latest = reports[-1] if reports else None
        return dict(
            repo=str(self.git.root), snapshot=await self.public_snapshot(self.current),
            baseline_id=baseline.id if baseline else None,
            preexisting_paths=[f.path for f in baseline.files if f.dirty] if baseline else [],
            retrospective=await self.store.setting("retrospective", True),
            report=latest, reports=[dict(id=r["id"], created_at=r["created_at"], snapshot_id=r["snapshot_id"]) for r in reports],
            report_stale=bool(latest and latest["snapshot_version"] != self.current.version),
            report_sources_stale=bool(latest and latest.get("evidence_version") != self.evidence_version(await self.events())),
            llm_available=self.llm.available, source_status=self.source_status,
            report_drafts=[{k: d[k] for k in ("id", "created_at", "snapshot_id", "status", "revision")}
                           for d in await self.store.list("report_draft") if d["status"] != "completed"],
            active_report_draft_id=self.active_report,
            selected_sessions=await self.store.setting("sessions", []),
            jobs=await self.store.list("job"),
            operations=[{k: v for k, v in op.items() if k not in ("desired_index",)} for op in await self.store.list("operation")],
        )

    @staticmethod
    async def public_snapshot(snapshot):
        return dict(id=snapshot.id, created_at=snapshot.created_at, version=snapshot.version,
                    head=snapshot.head, fragments=await asyncio.to_thread(diff_fragments, snapshot),
                    untracked=snapshot.omitted_untracked)

    async def sessions(self):
        async def read(provider, adapter):
            try:
                sessions = await adapter.sessions()
                self.source_status[provider] = {"ok": True, "message": "Connected"}
                return sessions
            except Exception as exc:
                self.source_status[provider] = {"ok": False, "message": str(exc)[:300]}
                return []
        results = await asyncio.gather(*(read(p, a) for p, a in self.adapters.items()))
        imported = await self.store.list("import")
        return dict(sessions=[s for batch in results for s in batch] + imported, status=self.source_status)

    async def sync(self):
        async with self.sync_lock:
            selected = await self.store.setting("sessions", [])
            for selection in selected:
                provider = selection["provider"]
                if provider not in self.adapters:
                    continue
                try:
                    events = await self.adapters[provider].events(selection["id"])
                    for event in events:
                        await self.store.event(event)
                    await self.store.set_setting(f"manifest:{provider}:{selection['id']}", [e.id for e in events])
                    self.source_status[provider] = {"ok": True, "message": f"{len(events)} events synchronized"}
                except Exception as exc:
                    self.source_status[provider] = {"ok": False, "message": str(exc)[:300]}
            await self.emit("sources", status=self.source_status)

    async def events(self):
        selected = {(s["provider"], s["id"]) for s in await self.store.setting("sessions", [])}
        manifests = {pair: await self.store.setting(f"manifest:{pair[0]}:{pair[1]}") for pair in selected}
        result = []
        for event in await self.store.list("event"):
            pair = ("import" if event["provider"] == "opencode-import" else event["provider"], event["session_id"])
            if event["provider"] == "decision" or (pair in selected and (manifests[pair] is None or event["id"] in manifests[pair])):
                result.append(event)
        return result

    async def import_history(self, body):
        session, events = import_opencode(body)
        for event in events:
            await self.store.event(event)
        await self.store.set_setting(f"manifest:import:{session['id']}", [e.id for e in events])
        await self.store.put("import", session["id"], session)
        selected = await self.store.setting("sessions", [])
        if not any(s["provider"] == "import" and s["id"] == session["id"] for s in selected):
            selected.append(session)
            await self.store.set_setting("sessions", selected)
        return session

    async def operation(self, req: OperationRequest, preview=False):
        async with self.git.lock:
            existing = await self.store.get("operation", req.key)
            request_hash = digest(req.model_dump_json().encode())
            if existing and not preview:
                if existing.get("request_hash") != request_hash:
                    raise ReviewError("Idempotency key already belongs to another operation", 409)
                return existing
            snapshot = await self.store.snapshot(req.snapshot_id)
            if not snapshot or snapshot.version != req.expected_version:
                raise ReviewError("Unknown or stale snapshot", 409)
            current = await self.refresh({req.path})
            if current.version != req.expected_version:
                raise ReviewError("Repository changed; refresh the diff", 409)
            file, target = await asyncio.to_thread(self.git.prepare_content, snapshot, req)
            if preview:
                return dict(path=file.path, exists=target.exists, content=target.bytes().decode("utf-8"),
                            changes_index=req.action != "discard")
            index, index_version = await self.git.build_index(snapshot, req.path, target) if req.action != "discard" else (None, snapshot.index_version)
            op = dict(id=req.key, request_hash=request_hash, path=req.path, action=req.action,
                      status="prepared", created_at=now(), before_id=snapshot.id, before_version=snapshot.version,
                      head=snapshot.head,
                      desired_index_hash=index_version,
                      desired_work_hash=(target if index is None else file.work).fingerprint())
            await self.store.put("operation", op["id"], op)
            try:
                await self.git.write(snapshot, file, target, index)
                after = await self.refresh({req.path})
                op.update(status="completed", after_id=after.id, after_version=after.version)
            except Exception as exc:
                await self.recover(op)
                recorded = await self.store.get("operation", op["id"])
                if recorded["status"] != "completed":
                    raise
                op = recorded
            await self.store.put("operation", op["id"], op)
            await self.emit("operation", operation=op)
            return op

    async def undo(self, op_id: str, key: str):
        async with self.git.lock:
            duplicate = await self.store.get("operation", key)
            if duplicate:
                if duplicate.get("undo_of") != op_id:
                    raise ReviewError("Idempotency key already used", 409)
                return duplicate
            op = await self.store.get("operation", op_id)
            if not op or op["status"] != "completed":
                raise ReviewError("Operation cannot be undone", 409)
            current = await self.refresh({op["path"]})
            if current.version != op["after_version"]:
                raise ReviewError("Repository changed after this operation; undo would overwrite newer work", 409)
            before = await self.store.snapshot(op["before_id"])
            old_file = next(f for f in before.files if f.path == op["path"])
            current_file = next(f for f in current.files if f.path == op["path"])
            changes_work = op["action"] == "discard" or op.get("changes_work", False)
            index = None if changes_work else base64.b64decode(before.index_content)
            target = old_file.work
            undo = dict(id=key, undo_of=op_id, path=op["path"], action="undo", changes_work=changes_work,
                        status="prepared", created_at=now(), before_id=current.id, before_version=current.version,
                        head=current.head,
                        desired_index_hash=before.index_version if index is not None else current.index_version,
                        desired_work_hash=(target if changes_work else current_file.work).fingerprint())
            await self.store.put("operation", key, undo)
            await self.git.write(current, current_file, target, index)
            after = await self.refresh({op["path"]})
            undo.update(status="completed", after_id=after.id, after_version=after.version)
            await self.store.put("operation", key, undo)
            await self.emit("operation", operation=undo)
            return undo

    async def start_job(self, kind: str, fn):
        if kind == "report" and self.active_report:
            raise ReviewError("A report is already being generated", 409)
        id = uid()
        if kind == "report":
            self.active_report = id
        job = dict(id=id, kind=kind, status="queued", created_at=now())
        try:
            await self.store.put("job", id, job)
        except BaseException:
            if kind == "report":
                self.active_report = None
            raise

        async def run():
            try:
                job["status"] = "running"
                await self.store.put("job", id, job)
                await self.emit("job", job=job)
                job["result"] = await fn(id)
                job["status"] = "completed"
            except asyncio.CancelledError:
                job.update(status="interrupted" if self.stopping else "cancelled",
                           error="Server stopped" if self.stopping else "Cancelled")
            except Exception as exc:
                error = (str(exc) if isinstance(exc, ReviewError)
                         else f"{type(exc).__name__}: {str(exc)[:400]}")
                job.update(status="failed", error=error)
            finally:
                await self.store.put("job", id, job)
                await self.emit("job", job=job)
                self.tasks.pop(id, None)
                if kind == "report":
                    self.active_report = None

        self.tasks[id] = asyncio.create_task(run())
        return job

    async def save_draft(self, draft):
        draft["revision"] += 1
        draft["updated_at"] = now()
        await self.store.put("report_draft", draft["id"], draft)
        # Queues must retain this revision, not a reference mutated by the next update.
        await self.emit("report_preview", job_id=draft["id"], revision=draft["revision"],
                        draft=json.loads(json.dumps(draft)))

    async def generate(self, job_id):
        try:
            result = await self._generate(job_id)
            draft = await self.store.get("report_draft", job_id)
            draft.update(status="completed", report_id=result["report_id"])
            await self.save_draft(draft)
            return result
        except BaseException as exc:
            draft = await self.store.get("report_draft", job_id)
            if draft:
                # Publishing the validated report is the commit point. Cancellation
                # immediately after that must not leave an apparently running draft.
                if await self.store.get("report", draft["report_id"]):
                    draft.update(status="completed")
                    await self.save_draft(draft)
                    return {"report_id": draft["report_id"]}
                if isinstance(exc, asyncio.CancelledError):
                    draft.update(status="interrupted" if self.stopping else "cancelled",
                                 error="Сервис остановлен. Генерация не завершена." if self.stopping else "Генерация отменена.")
                else:
                    draft.update(status="failed", error=str(exc) if isinstance(exc, ReviewError)
                                 else "Не удалось завершить отчёт.")
                await self.save_draft(draft)
            raise

    async def _generate(self, job_id):
        async with self.git.lock:
            snapshot = await self.refresh()
        fragments = await asyncio.to_thread(diff_fragments, snapshot)
        events = await self.events()
        baseline = await self.store.snapshot(await self.store.setting("baseline"))
        preexisting = sorted({f.path for f in baseline.files if f.dirty} |
                             (set(baseline.omitted_untracked) & self.git.selected_untracked)) if baseline else []
        scope = dict(preexisting_paths=preexisting, retrospective=await self.store.setting("retrospective", True),
                     note="Preexisting changes are not automatically attributable to the agent. Missing historical rationale must be marked as unknown or reconstructed.")
        draft = dict(id=job_id, report_id=uid(), is_draft=True, created_at=now(), revision=0,
                     status="running", snapshot_id=snapshot.id, snapshot_version=snapshot.version,
                     baseline_id=baseline.id if baseline else None, preexisting_paths=preexisting,
                     retrospective=scope["retrospective"], evidence_version=self.evidence_version(events),
                     source_ids=[e["id"] for e in events], summary="", items=[], findings=[])
        await self.store.put("report_draft_evidence", job_id, {"events": events})
        await self.save_draft(draft)

        async def progress(message):
            await self.emit("progress", job_id=job_id, message=message)

        async def on_preview(data):
            draft.update(data)
            await self.save_draft(draft)

        generated, details = await self.llm.report(fragments, events, progress, scope=scope, on_preview=on_preview)
        covered = {fid for item in generated.items for fid in item.fragment_ids}
        missing = [f for f in fragments if f["id"] not in covered]
        for file_layer, group in self.group_fragments(missing).items():
            generated.items.append(ReviewItem(section="Без объяснения", title=file_layer,
                                              explanation="Этот фрагмент не объяснён моделью. Проверьте diff вручную.",
                                              fragment_ids=[f["id"] for f in group],
                                              limitations=["Обоснование отсутствует"]))
        # Carry marks only for byte-for-byte identical explanations and fragment references.
        previous = await self.store.list("report")
        reviewed = set()
        if previous:
            for item in previous[-1]["items"]:
                if item.get("reviewed"):
                    reviewed.add(self.item_signature(item))
        for item in generated.items:
            item.id = uid()
            item.reviewed = self.item_signature(item.model_dump()) in reviewed
        report = dict(id=draft["report_id"], created_at=now(), snapshot_id=snapshot.id, snapshot_version=snapshot.version,
                      baseline_id=baseline.id if baseline else None,
                      preexisting_paths=preexisting, retrospective=scope["retrospective"],
                      evidence_version=self.evidence_version(events),
                      source_ids=[e["id"] for e in events], **generated.model_dump(), **details)
        await self.store.put("report_evidence", report["id"], {"events": events})
        await self.store.put("report", report["id"], report)
        return dict(report_id=report["id"])

    @staticmethod
    def evidence_version(events):
        return digest(json.dumps([(e["id"], e["text"], e["role"]) for e in events], sort_keys=True).encode())

    @staticmethod
    def item_signature(item):
        return json.dumps({k: v for k, v in item.items() if k not in ("id", "reviewed")}, sort_keys=True)

    @staticmethod
    def group_fragments(fragments):
        result = defaultdict(list)
        for fragment in fragments:
            result[f"{fragment['path']} · {fragment['layer']}"].append(fragment)
        return result

    async def chat(self, job_id, question, snapshot_id, report_id, item_id):
        snapshot = await self.store.snapshot(snapshot_id)
        if not snapshot:
            raise ReviewError("Snapshot not found", 404)
        report = await self.store.get("report", report_id) if report_id else None
        item = next((i for i in report["items"] if i["id"] == item_id), None) if report and item_id else None
        if report and report["snapshot_id"] != snapshot_id:
            raise ReviewError("Report does not match the requested snapshot")
        if item_id and not item:
            raise ReviewError("Review item not found", 404)
        fragments = await asyncio.to_thread(diff_fragments, snapshot)
        if item:
            relevant = set(item["fragment_ids"] + item.get("dependencies", []))
            fragments = [f for f in fragments if f["id"] in relevant]
        events = await self.events()
        if report:
            evidence = await self.store.get("report_evidence", report["id"])
            events = evidence["events"] if evidence else []
        thread = f"{snapshot_id}:{item_id or 'all'}"
        history = [m for m in await self.store.list("message") if m["thread"] == thread and m.get("complete", True)]
        user = dict(id=uid(), thread=thread, role="user", content=question, created_at=now(), snapshot_id=snapshot_id)
        await self.store.put("message", user["id"], user)
        message = dict(id=uid(), thread=thread, role="assistant", content="", created_at=now(), snapshot_id=snapshot_id, complete=False)
        await self.store.put("message", message["id"], message)
        try:
            async for delta in self.llm.answer(question, fragments, events, history, item):
                message["content"] += delta
                await self.emit("chat_delta", job_id=job_id, message_id=message["id"], delta=delta, thread=thread)
            message["complete"] = True
        finally:
            await self.store.put("message", message["id"], message)
        return dict(message_id=message["id"])

    async def poll(self):
        while True:
            await asyncio.sleep(self.config.poll_interval)
            try:
                async with self.git.lock:
                    await self.refresh()
                await self.sync()
            except Exception as exc:
                await self.emit("warning", message=str(exc)[:300])
