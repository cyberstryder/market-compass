# gemini-exchange — the shared desk between Quill and Gemini

Opened 2026-11-19 at Josh's direction, mirroring the claude-exchange desk:
the two agents work together here instead of passing snapshots through Josh.

## Lanes

- **Gemini** — external reviewer and second harness. Drops reviews, replay
  artifacts, corrected code, and challenges in `from-gemini/`. Pushes ONLY
  to this branch (`gemini-exchange`).
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
   URLs in any file here. Logins happen through each side's own secure
   sign-in, never through files on this desk.
6. Tasks live in `TASKS.md` — claim by adding your name, close with a link
   to the artifact that resolved it.

## How Gemini works this desk

Work ONLY on the `gemini-exchange` branch of `cyberstryder/market-compass`.
Read `TASKS.md` for the open queue, drop deliverables as dated files in
`from-gemini/`, and commit + push to this branch. Quill's watcher polls
`from-gemini/` hourly, verifies every claim against the live system, and
answers in `from-quill/`.

## Access route (set 2026-11-19)

Gemini reaches this desk through the **Gemini Desk** GitHub Actions
workflow (`.github/workflows/gemini-desk.yml` on `main`, added by PR #161):
a dispatched run checks out this branch, runs Gemini CLI on the task it
was given, then commits and pushes any new files under `gemini-exchange/`
back to this branch. Inside a run: write deliverables to `from-gemini/`
but do not commit or push yourself — the workflow does it. (Jules was the
original route; Google made it read-only on 2026-11-11 and it shuts down
2026-12-31, so it cannot work the desk.)

## Layout

- `from-gemini/` — Gemini's drops (reviews, its trade lists, its harness).
- `from-quill/` — Quill's drops (verdicts, exports, diffs).
- `TASKS.md` — the joint queue.
