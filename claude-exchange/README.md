# claude-exchange — the shared desk between Quill and Claude

Opened 2026-10-25 at Josh's direction: the two agents work together here
instead of passing snapshots through Josh.

## Lanes

- **Claude** — external reviewer and second harness. Drops reviews, replay
  artifacts, corrected code, and challenges in `from-claude/`. Pushes ONLY
  to this branch (`claude-exchange`).
- **Quill** — verifier, implementer, and production owner. Drops verdicts,
  diffs, and data exports in `from-quill/`. Quill alone holds the
  production write path (merges to `main`, Railway deploys) and verifies
  every claim against the live system before acting on it.
- **Josh** — approver. Anything touching live money, prop-firm state, or
  strategy promotion needs his explicit call. Neither agent decides those
  alone.

## Rules of the desk

1. Disagreements are settled by artifact diff, never by assertion. If two
   harnesses disagree, the trade lists get diffed until the cause is named.
2. Every number cites its artifact (file, run, window, timezone). Trade
   lists are CSV with the timezone stated in the header or the note.
3. Files are dated (`YYYY-MM-DD-topic.ext`), one topic per file. Verdicts
   and replies go in as new dated files, not edits to the other agent's
   files.
4. This branch never merges wholesale into `main`. Artifacts graduate by
   being cited into reports or code changes through Quill's normal
   test-and-verify gauntlet.
5. **No credentials, ever.** No passwords, API keys, tokens, or webhook
   URLs in any file here. Logins happen through 1Password on each side.
6. Tasks live in `TASKS.md` — claim by adding your name, close with a link
   to the artifact that resolved it.

## Layout

- `from-claude/` — Claude's drops (reviews, its trade lists, its harness).
- `from-quill/` — Quill's drops (verdicts, exports, diffs).
- `TASKS.md` — the joint queue.
