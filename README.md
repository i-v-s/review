# Review service

A local browser workspace for reviewing uncommitted changes from Codex and OpenCode.
Python 3.12+, aiohttp, SQLite, the OpenAI Python SDK, and vanilla JavaScript.
The interface and generated explanations are in Russian.

## Run

```bash
python3 -m venv env
env/bin/python -m pip install -e '.[test]'
cp .env.example .env
# Edit .env with your provider key and model.
env/bin/review-service --repo /absolute/path/to/git/repository
```

Open `http://127.0.0.1:8765`. The command prints the path to a private **browser access
token file**; copy its contents into the login form. This is separate from the LLM key.
An explicit `REVIEW_TOKEN` can be used instead. The service reads `.env` from the
current directory at startup, including for the `decision` command. Existing process
environment variables take precedence. Restart after editing `.env`.

The reviewed repository must already exist. The service does not initialize or commit
repositories. It supports Linux and macOS (uses `fcntl` for the repository process lock).
Run one instance per worktree. `--state-dir` must be outside the reviewed repository;
the default is `$XDG_STATE_HOME/review-service/<repo-hash>` or
`~/.local/state/review-service/<repo-hash>`. State includes private source history and
recovery copies. Keep it until review and recovery are no longer needed.

Without LLM configuration, Git operations, snapshots, source import and saved reports
still work. No background LLM requests are made. Report generation and questions use
Chat Completions, so the provider must implement streaming requests
with `model`, `messages`, and `max_tokens`. Reports use JSON Schema structured output
by default through `response_format: {type: "json_schema", json_schema: {name,
strict: true, schema}}`, supported by llama-server. Each request's schema restricts
source and fragment references to the evidence actually supplied in that request.
The server still validates the result and permits one repair attempt. Validation
errors identify the failed portion and field; failed generations preserve the draft
and previous report.

For a provider without JSON Schema support, set `REVIEW_LLM_STRUCTURED_OUTPUT=0`
and restart. Only `0` and `1` are accepted. This omits `response_format` while keeping
the schema in the prompt and all local checks. A provider error never silently disables
structured output. Questions continue to use plain-text streaming.

Use **Модель для отчётов и чата** to choose a model from the configured provider's
list or enter its ID, then select **Применить**. The list is fetched after login and
with **Обновить список**, with a ten-second timeout and no transport retries.
If the catalog is empty or unavailable, manual entry remains available. LLM credentials
and proxy configuration stay on the server; the browser receives model IDs only.
Listing models does not generate completions.

The choice applies to new reports and questions and is saved per repository and provider
URL across browser reloads and service restarts. It overrides `REVIEW_MODEL` until
**Из окружения** resets it. `REVIEW_MODEL` can be omitted if a model is chosen in the UI;
the API key and provider URL must still be configured on the server. Changing the model
does not alter running jobs: every portion and repair uses the model selected when the
job was accepted. Reports, drafts and chat answers show the model used; older artifacts
without model metadata remain readable. Other connected tabs receive selection updates.

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
to report generation, questions and model discovery only; session adapters and Git use their own
connections. A failed proxy connection does not fall back to a direct connection.
Proxy credentials are not included in browser state or user-facing error messages.
An empty setting keeps the existing transport. Update an existing installation with
`env/bin/python -m pip install -e '.[test]'` to install the SOCKS dependency.

## Workflow

The left sidebar shows the workspace menu or, when a report or draft is selected,
the report tree. **WORKSPACE / Обзор** returns to the menu. Report sections and their
file lists start collapsed; their expanded state and the sidebar width are saved in
the browser for each repository (and, for tree sections, each report version). Drag
the sidebar edge or use its arrow keys to change its width.

The tree omits staged file entries. Each working file shows added and removed diff
line counts. Test and documentation files appear only after checking **Тесты** or
**Документация**. To replace the built-in path rules, create `.review.toml` in the
reviewed repository:

```toml
[file_filters]
tests = ["tests/*", "*/tests/*", "*.test.*"]
docs = ["docs/*", "*.md"]
```

Both lists are required; either may be empty. Patterns match the full relative path
without regard to letter case: `*` matches any number of characters, including `/`,
and `?` matches one character. A path matching both lists is treated as a test.
Without `.review.toml`, the service recognizes common test names and `tests/`
directories, plus `docs/`, README, Markdown, reStructuredText, and AsciiDoc files.
Restart the service after changing `.review.toml`.

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
5. Review decisions, ask questions, and select individual added/deleted lines. In the
   report diff, Stage works in read-only mode. Enable **Staged** to compare HEAD with
   the working file, see staged lines separately, and Stage/Unstage selected lines.
   Selecting a replacement in this view includes both sides of that replacement.
6. Stage/unstage selected changes, or preview and discard working changes. Actions
   made from a report item appear in that item's journal; **История действий** also
   lists Git actions and provides undo while the repository matches their result.

Git state and selected session histories are checked every five seconds. Code on screen
stays at its snapshot until refreshed. A stale snapshot cannot mutate Git. The report's
original snapshot and conversation context remain immutable; report actions advance a
separate working snapshot for the current diff. Report generation creates a new version;
older reports and their conversations are retained.
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
| `GET /models`, `PUT /model` | Discover provider model IDs; select a model for new reports and questions |
| `GET/PUT /sessions`, `POST /import`, `GET /sources` | Discover/select sessions, import OpenCode history, inspect evidence |
| `POST /reviews`, `POST /untracked`, `POST /decisions` | Start a baseline, include new files, record a decision |
| `GET /snapshots/{id}`, `GET /snapshots/{id}/file`, `GET /snapshots/{id}/comparison` | Immutable diff, file context and HEAD-to-work line mappings (`path`, plus `side` for file content) |
| `POST /reports`, `GET /reports/{id}` | Start generation (202/job), retrieve a report with its working snapshot and item journal |
| `GET /report-drafts/{job_id}` | Retrieve a saved partial report, its status and revision |
| `PATCH /reports/{id}/items/{item}` | Set `reviewed` independently of staging |
| `POST /operations/preview`, `POST /operations`, `POST /operations/edit` | Preview/apply selected changes or save a working file |
| `POST /operations/{id}/undo` | Restore a recorded operation using a new idempotency `key` |
| `POST /questions`, `GET /messages` | Ask about a snapshot/item, retrieve conversation |
| `GET /events`, `POST /jobs/{id}/cancel` | SSE job/delta notifications and cancellation |

`/state` includes `report_drafts` (unfinished draft metadata) and
`active_report_draft_id`. SSE `report_preview` events contain `job_id`, a monotonically
increasing `revision`, and a full `draft` replacement. Clients can restore the latest
saved version after reconnecting or receiving `resync`. Draft statuses are `running`,
`completed`, `failed`, `cancelled`, and `interrupted`; completed drafts link to their
published `report_id`. `/sources?draft_id=JOB_ID` retrieves the draft's frozen sources.

`/state` also includes `llm_model`, `llm_default_model` (the startup `REVIEW_MODEL`)
and `llm_configured` (whether a provider API key is configured). `llm_available`
requires both the provider configuration and a selected model. `GET /models` returns
`{models: [ID, ...]}`; catalog errors do not change the selection. `PUT /model`
accepts `{model: ID}` (a nonempty string of at most 1024 characters) or `{model: null}`
to reset to the environment default, and returns `{model: effective_ID}`. An ID need
not appear in the catalog. Successful changes emit a `model_changed` SSE event;
clients reload `/state`. New report/chat jobs, drafts, reports and assistant messages
include the frozen `model` ID. These are additive fields; no database migration is needed.

An operation supplies `snapshot_id`, `expected_version`, `path`, `action`
(`stage`, `unstage`, `discard`), `line_ids`, `whole_file` and a unique `key`.
Git operations and file edits may also supply `report_id`, `target_kind`
(`summary`, `item` or `finding`) and `target_id` to advance the report's working
snapshot and record a scoped journal entry. Undo inherits that scope.
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
