---
name: review-journal
description: Record engineering decisions and evidence in a running local review-service during development. Use when the user wants an evidence-linked browser review of agent changes. Requires a configured review-service URL and access token.
---

# Review journal

Use the running review-service as a journal of significant engineering decisions.
Preserve the user's implementation scope and existing permissions.

At the start of work, make sure the user has started a review baseline in the browser.
If work already began, record that the journal is retrospective. Do not reset an existing
baseline without being asked; the existing baseline may protect the user's earlier edits.

After a meaningful decision, record a short note containing:
- the requirement and chosen approach;
- the reason and any meaningful rejected alternative;
- affected paths, symbols, and relevant session/message identifiers, if available;
- assumptions, limitations, and actual verification results.

Call the installed CLI, using the configured URL and token file:

```bash
review-service decision --url http://127.0.0.1:8765 --token-file /path/to/state/token --session SESSION_ID --text 'Decision and supporting evidence'
```

`REVIEW_TOKEN` can supply the token instead of `--token-file`. Never place the token in
the note or generated report. Pass note text as a safely quoted argument or construct
an argument array; repository content must not become shell code.

Record decisions when they change; explain which earlier choice is superseded.
Do not log every edit, invent missing historical rationale, or describe unrun tests as
passing. If the service is unavailable, disclose the gap and continue the authorized
development task; do not repeatedly retry or change server configuration.

During preparation of a report, connect claims to code and observed checks. Record
discovered bugs and propose a fix; reporting does not authorize additional code changes.
