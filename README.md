# Review service

A local browser workspace for reviewing uncommitted changes from Codex and OpenCode.
Python 3.12+, aiohttp, SQLite, the OpenAI Python SDK, and vanilla JavaScript.
The interface and generated explanations are in Russian.

## Run

```bash
python3 -m venv env
env/bin/python -m pip install -e '.[test]'
export OPENAI_API_KEY='your-provider-key'
export OPENAI_BASE_URL='https://api.openai.com/v1'
export REVIEW_MODEL='your-model-id'
env/bin/review-service --repo /absolute/path/to/git/repository
```

Open `http://127.0.0.1:8765`. The command prints the path to a private **browser access
token file**; copy its contents into the login form. This is separate from the LLM key.
An explicit `REVIEW_TOKEN` can be used instead. See `.env.example`; environment files
are not automatically loaded.

The reviewed repository must already exist. The service does not initialize or commit
repositories. It supports Linux and macOS (uses `fcntl` for the repository process lock).
Run one instance per worktree. `--state-dir` must be outside the reviewed repository;
the default is `$XDG_STATE_HOME/review-service/<repo-hash>` or
`~/.local/state/review-service/<repo-hash>`. State includes private source history and
recovery copies. Keep it until review and recovery are no longer needed.

Without LLM configuration, Git operations, snapshots, source import and saved reports
still work. No background LLM requests are made. Report generation and questions use
Chat Completions, so the provider must implement streaming requests
with `model`, `messages`, and `max_tokens`. JSON schema support is not required.

An LLM request may wait for 600 seconds by default. For reports, this also limits the
entire response stream for each attempt, including any configured transport retries.
One JSON repair attempt has its own deadline. Set `REVIEW_LLM_TIMEOUT_SECONDS`
before starting the service to change that limit. Automatic transport retries are
disabled so one slow request does not silently multiply the wait or its possible cost;
set `REVIEW_LLM_MAX_RETRIES` if the provider needs them. For a consistently slow
provider, `REVIEW_MAX_CONTEXT_CHARS` can reduce the evidence sent in each request.
These values are read at startup.

To route LLM requests through a SOCKS5 proxy, set:

```bash
export REVIEW_LLM_PROXY='socks5://127.0.0.1:1080'
# With authentication (socks5h is accepted too):
export REVIEW_LLM_PROXY='socks5h://user:password@127.0.0.1:1080'
```

Restart the service after changing this setting. Percent-encode special characters
in credentials, for example `user%40name:pass%3Aword`. The port is required. Both
schemes send the provider hostname to the proxy for resolution. This setting applies
to report generation and questions only; session adapters and Git use their own
connections. A failed proxy connection does not fall back to a direct connection.
Proxy credentials are not included in browser state or user-facing error messages.
An empty setting keeps the existing transport. Update an existing installation with
`env/bin/python -m pip install -e '.[test]'` to install the SOCKS dependency.

## Workflow

1. Before development, select **Начать новую работу**. Current changes become the
   baseline. If you connect later, the report is marked retrospective.
2. In **Источники**, select Codex/OpenCode sessions, import an OpenCode export, or
   record a decision. Session selection limits what is sent to the LLM. The small
   `skills/review-journal` skill can be installed into your agent's skills directory.
3. In **Изменения**, explicitly include any untracked files that belong to the review.
   Ignored files are not discovered automatically.
4. Select **Создать отчёт**. Explanations cite stored sources and immutable diff
   fragments. The summary appears during generation; complete decisions and findings
   appear after their structure and references are checked. The **Предпросмотр** is
   read-only until the full report passes validation. Every fragment in the finished
   report is represented, including an explicit unexplained section.
5. Review decisions, ask questions, and select individual added/deleted lines. For a
   replacement, select both its deletion and addition if you want the whole replacement.
6. Stage/unstage selected changes, or preview and discard working changes. **История
   действий** provides undo when the repository still matches the operation's result.

Git state and selected session histories are checked every five seconds. Code on screen
stays at its snapshot until refreshed. A stale snapshot cannot mutate Git. Report
generation creates a new version; older reports and their conversations are retained.
Review marks are carried forward only for identical explanations and fragment references.

Drafts retain their own frozen snapshot and source evidence. Refreshing the browser
or reconnecting restores the saved preview. Cancellation, a timeout, a broken stream,
or a server restart leaves an accessible **Не завершён** draft alongside previous
finished reports. An interrupted generation is not resumed: generating again creates
a new draft. During JSON repair, the current portion is replaced and previously
validated portions remain. Streaming updates are coalesced every 250 ms; the final
available preview is saved immediately when generation stops. A provider must send
the normal `finish_reason: "stop"` to complete a report; a token limit or incomplete
stream never publishes a finished report.

**Semantics:** stage changes the index only; unstage restores selected index changes
towards HEAD; discard restores selected working changes towards the index. Operations
are per file. Stage does not commit. Undo rejects newer repository changes rather than
overwriting them. A write-ahead journal retains backups across restarts; an interrupted
operation is recovered only if its expected resulting state can be verified.

## Session adapters

**Codex:** install `codex` on PATH. The adapter starts `codex app-server --listen
stdio://`, initializes JSON-RPC and reads `thread/list` and `thread/read`. It never
resumes or runs a development turn. The app-server must expose the selected saved
session under the same user and working directory. Availability of desktop/cloud
sessions depends on the installed client; an unavailable adapter is shown in the UI.

**OpenCode:** point `OPENCODE_URL` to a local server:

```bash
opencode serve --hostname 127.0.0.1 --port 4096
export OPENCODE_URL='http://127.0.0.1:4096'
```

If protected, set `OPENCODE_SERVER_USERNAME` and `OPENCODE_SERVER_PASSWORD` for the
review service too. The adapter targets the V1 HTTP session/message API, as supplied by
OpenCode 1.x. Alternatively, run `opencode export SESSION_ID > session.json` and import
it in the UI. Imported history is static; live sources are incrementally synchronized.

An explicit decision can be submitted without an adapter:

```bash
env/bin/review-service decision --session my-session \
  --token-file /path/to/review-state/token \
  --text 'Keep retries limited to GET. Tests cover timeout and HTTP 503; POST retries remain disabled.'
```

## Phone / HTTPS

Use a hostname reachable from the phone and a certificate trusted by that device:

```bash
env/bin/review-service --repo /path/to/repo --host 0.0.0.0 --port 8765 \
  --tls-cert /path/to/cert.pem --tls-key /path/to/key.pem
```

Non-loopback listeners require TLS. Open the HTTPS URL and enter the same browser
token. All line-selection and Git actions work on mobile. The repository machine must
remain running. The token is stored in sessionStorage for that browser tab; LLM credentials
are never sent to the frontend. Requests carry Authorization headers, not URL tokens.

## Tests

```bash
env/bin/python -m pytest -m 'not browser' -q
env/bin/python -m playwright install chromium
env/bin/python -m pytest -m browser -q
```

Browser tests may use an installed Chrome instead of downloading Chromium:
`REVIEW_TEST_BROWSER=/usr/bin/google-chrome env/bin/python -m pytest -m browser -q`.
Tests use temporary Git repositories and a local simulated LLM endpoint. No real
provider credentials or paid calls are required. A sandbox must allow local sockets,
subprocesses and asyncio's thread wakeups.

For a disposable demonstration with real Git changes:

```bash
env/bin/python examples/create_demo.py /tmp/review-demo
env/bin/review-service --repo /tmp/review-demo
```

Import `examples/opencode-session.json` and include `test_retry.py` in the review.
The example deliberately leaves a requirement about retry counts ambiguous. The
generator refuses to reuse an existing destination directory.

## API

All `/api/v1` routes require `Authorization: Bearer <browser-token>`.

| Routes | Purpose |
| --- | --- |
| `GET /state`, `POST /sync` | Current snapshot, reports, jobs and operation journal |
| `GET/PUT /sessions`, `POST /import`, `GET /sources` | Discover/select sessions, import OpenCode history, inspect evidence |
| `POST /reviews`, `POST /untracked`, `POST /decisions` | Start a baseline, include new files, record a decision |
| `GET /snapshots/{id}`, `GET /snapshots/{id}/file` | Immutable diff and file context (`path`, `side` query parameters) |
| `POST /reports`, `GET /reports/{id}` | Start generation (202/job), retrieve a report |
| `GET /report-drafts/{job_id}` | Retrieve a saved partial report, its status and revision |
| `PATCH /reports/{id}/items/{item}` | Set `reviewed` independently of staging |
| `POST /operations/preview`, `POST /operations` | Preview/apply selected changes |
| `POST /operations/{id}/undo` | Restore a recorded operation using a new idempotency `key` |
| `POST /questions`, `GET /messages` | Ask about a snapshot/item, retrieve conversation |
| `GET /events`, `POST /jobs/{id}/cancel` | SSE job/delta notifications and cancellation |

`/state` includes `report_drafts` (unfinished draft metadata) and
`active_report_draft_id`. SSE `report_preview` events contain `job_id`, a monotonically
increasing `revision`, and a full `draft` replacement. Clients can restore the latest
saved version after reconnecting or receiving `resync`. Draft statuses are `running`,
`completed`, `failed`, `cancelled`, and `interrupted`; completed drafts link to their
published `report_id`. `/sources?draft_id=JOB_ID` retrieves the draft's frozen sources.

An operation supplies `snapshot_id`, `expected_version`, `path`, `action`
(`stage`, `unstage`, `discard`), `line_ids`, `whole_file` and a unique `key`.
Line IDs come from the returned diff and are scoped to its snapshot, file and layer.
Retries with the same key return the recorded result; reusing the key with different
parameters is rejected. Conflicts return HTTP 409. The server never accepts a patch
or shell command from the browser or LLM.

## v0.1 boundaries

- UTF-8 regular files support line operations, including CRLF and missing final LF.
  Binary files, submodules, symlinks, content conversions (`filter`, `text`, `eol`,
  `working-tree-encoding`, `core.autocrlf`), file-mode changes and conflicted indexes
  are view-only. Renames are represented as deletion/addition, not atomic rename actions.
- Files over 2 MB are listed with omitted content. Context budgets may omit some history
  or oversized fragments; omissions are disclosed, and fragments stay available in Git review.
- Tests recorded in session history are evidence about their original execution, not
  a guarantee about the current staged tree. The service does not run repository tests
  or automatically apply model-suggested fixes.
- Operations serialize inside this service and respect Git's index lock. External
  editors/agents do not participate in that lock: pause them while discarding files.
  The service checks versions before writing but cannot provide a filesystem-wide
  transaction against uncooperative concurrent writers.
- A passing report schema checks structure and reference existence; the model can still
  misinterpret evidence. Review its claims and the linked code.
- No cloud hosting, multiuser permissions, automatic commits, or background autonomous
  development. For LLM analysis the selected context is sent to your configured provider.
