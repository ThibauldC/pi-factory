# pi compatibility notes

Findings from running the **real pi 1.5.0** (`~/.pi/agent/bin/pi`) on the VM, read-only, in a
scratch directory with its own `--session-dir` (task 4.3, 2026-11-10). The design's protocol
notes came from the 1.5.1 docs; everything the factory relies on is confirmed unchanged in
1.5.0. The opt-in test `PI_FACTORY_SMOKE=1 uv run pytest tests/test_smoke_pi.py` re-checks the
points below against whatever pi version is installed (it sends a few trivial prompts, a few
thousand tokens in total).

## Confirmed

| Assumption (design D6) | Result on 1.5.0 |
|---|---|
| RPC framing | JSONL on stdin/stdout, LF-terminated. Diagnostics go to stderr. |
| `prompt` | Responds `{"type":"response","command":"prompt","success":true,"data":{"disposition":"started"}}` and echoes the command `id`. |
| End of a turn | `agent_end` is followed by `agent_settled` with an `aborted` flag; the factory waits for `agent_settled`. |
| Final text | `get_last_assistant_text` returns `data.text`. |
| Session file | `get_state` returns `data.sessionFile`; `get_session_stats` returns `sessionFile` and a `tokens` object. |
| Usage | Each assistant `message_end` carries `model` and `usage` (`input`, `output`, `cacheRead`, `cacheWrite`, `totalTokens`, plus cost). The factory sums these per model per turn. |
| `--session-dir <d> --session-id <id>` | Creates the session when none exists (stderr says `Warning: No project session found with id '<id>'; creating a new session with that id.`, which is not an error) and reopens the *same* file on the next start: `<d>/<timestamp>_<id>.jsonl`. The reopened session remembered the earlier turn. |
| Killed mid-turn | After SIGKILL during streaming, the file holds the user message but no assistant message. Reopening it works, the old user message stays, and a new prompt is answered normally. |
| `--tools read,grep,find,ls` | The session's system message lists exactly those four tools (`toolsAdded`); `bash`, `edit` and `write` are absent. The user's extension `grep` (pi-fff) is available under that name. |
| `--no-approve` | Accepted. |
| stdin close | Orderly shutdown, exit code 0. |

## Things the fake pi now mimics

- User extensions announce themselves at startup through `extension_ui_request` records
  (`setStatus`, `notify`) before and during the first turn. The factory logs them and never
  needs to answer them (only `confirm`, `select`, `input` and `editor` need replies, and those
  get automatic answers).
- Session files are named `<ISO timestamp>_<session id>.jsonl`.
- A missing session prints the `No project session found` warning on stderr.

## Not tested against the real pi

- Provider failures (`stopReason: "error"`), `length` stops and corrupt session files: covered
  by the fake only.
- The 45-minute timeout and abort escalation with a real model: covered by the fake only.

## Operational note

Real pi runs refresh the shared `openai-codex` login in `~/.pi/agent/auth.json` like any pi
run, and pi-relay uses the same login. Nothing else under `~/.pi/agent` was touched by the
scratch runs (no new directory appeared under `~/.pi/agent/sessions`).
