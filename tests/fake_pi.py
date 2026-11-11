#!/usr/bin/env python3
"""A fake ``pi --mode rpc`` for tests. Speaks the JSONL protocol documented for pi 1.5.0.

Behaviour is scripted by a JSON file named in $FAKE_PI_SCRIPT::

    {"log": "/path/fake-pi-log.jsonl", "turns": [ {turn}, {turn}, ... ]}

The n-th prompt received (counted across process lifetimes through the log file) uses
``turns[n]``; past the end, the last entry repeats. A turn is::

    behavior   success (default) | error | hang | hang_deaf | crash | ui_confirm | ui_select
    text       final assistant text
    edits      {relative path: content} written in the working directory
    usage      {"input": n, "output": n, "cacheRead": n, "cacheWrite": n}
    error_message, stop_reason, sleep, dump_env (relative path), tool_calls, exit_code
"""

from __future__ import annotations

import json
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path

if "--version" in sys.argv:
    print("fake-pi 1.2.3")
    sys.exit(0)

SCRIPT = json.loads(Path(os.environ["FAKE_PI_SCRIPT"]).read_text())
LOG = Path(SCRIPT["log"])
MODEL = "fake-model"


def argval(flag):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None


def log(entry):
    with open(LOG, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def prompts_so_far():
    if not LOG.exists():
        return 0
    return sum(
        1 for line in LOG.read_text().splitlines() if json.loads(line).get("event") == "prompt"
    )


session_dir = Path(argval("--session-dir") or ".")
session_id = argval("--session-id") or "anon"
session_dir.mkdir(parents=True, exist_ok=True)
existing = sorted(session_dir.glob(f"*_{session_id}.jsonl"))
if existing:
    session_file = existing[0]
    if "CORRUPT" in session_file.read_text():
        sys.stderr.write(f"Error: failed to load session {session_file}: unexpected token\n")
        sys.exit(1)
    resumed = True
else:
    sys.stderr.write(
        f"Warning: No project session found with id '{session_id}'; "
        "creating a new session with that id.\n"
    )
    session_file = session_dir / f"2026-01-01T00-00-00-000Z_{session_id}.jsonl"
    session_file.write_text(
        json.dumps({"type": "session", "id": session_id, "cwd": os.getcwd()}) + "\n"
    )
    resumed = False

log(
    {
        "event": "start",
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "env": dict(os.environ),
        "resumed": resumed,
    }
)

commands: queue.Queue = queue.Queue()

# Like the user's real extensions, announce ourselves through the extension-UI sub-protocol.
out(
    {
        "type": "extension_ui_request",
        "id": "boot-1",
        "method": "setStatus",
        "statusKey": "ext",
        "statusText": "loaded",
    }
)
out(
    {
        "type": "extension_ui_request",
        "id": "boot-2",
        "method": "notify",
        "message": "extension loaded",
        "notifyType": "info",
    }
)


def read_stdin():
    for line in sys.stdin:
        if line.strip():
            commands.put(json.loads(line))
    commands.put(None)  # EOF


threading.Thread(target=read_stdin, daemon=True).start()

totals = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
last_text = [None]
msg_count = [0]


def respond(cmd, data=None, success=True, error=None):
    rec = {"type": "response", "command": cmd.get("type"), "success": success}
    if cmd.get("id"):
        rec["id"] = cmd["id"]
    if data is not None:
        rec["data"] = data
    if error:
        rec["error"] = error
    out(rec)


def assistant_message(text, usage, stop="stop", error=None):
    msg = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}] if text else [],
        "model": MODEL,
        "provider": "fake",
        "usage": {**usage, "totalTokens": sum(usage.values())},
        "stopReason": stop,
    }
    if error:
        msg["errorMessage"] = error
    return msg


def wait_for(timeout, accept):
    """Wait for a queued command satisfying ``accept``; handle abort/EOF. Returns it or None."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            cmd = commands.get(timeout=0.05)
        except queue.Empty:
            continue
        if cmd is None:
            sys.exit(0)
        if accept(cmd):
            return cmd
    return None


def settle(msg, aborted=False):
    out({"type": "message_end", "message": msg})
    out({"type": "turn_end", "message": msg, "toolResults": []})
    out({"type": "agent_end", "messages": [msg], "willRetry": False})
    out({"type": "agent_settled", "aborted": aborted})
    last_text[0] = "".join(b["text"] for b in msg["content"] if b.get("type") == "text") or None


def run_turn(cmd):
    index = prompts_so_far()
    turns = SCRIPT.get("turns") or [{}]
    spec = turns[min(index, len(turns) - 1)]
    log({"event": "prompt", "message": cmd["message"], "index": index})
    with open(session_file, "a") as fh:
        fh.write(json.dumps({"type": "message", "role": "user", "content": cmd["message"]}) + "\n")
    respond(cmd, {"disposition": "started"})
    out({"type": "agent_start"})
    out({"type": "turn_start"})
    behavior = spec.get("behavior", "success")
    text = spec.get("text", "done")
    usage = {"input": 10, "output": 5, "cacheRead": 0, "cacheWrite": 0, **spec.get("usage", {})}

    if spec.get("sleep"):
        time.sleep(spec["sleep"])
    for tool in spec.get("tool_calls", []):
        out(
            {"type": "tool_execution_start", "toolCallId": "t1", "toolName": tool, "args": {"x": 1}}
        )
        out(
            {
                "type": "tool_execution_end",
                "toolCallId": "t1",
                "toolName": tool,
                "result": "ok",
                "isError": False,
            }
        )
    for rel, content in spec.get("edits", {}).items():
        target = Path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    if spec.get("dump_env"):
        Path(spec["dump_env"]).write_text(json.dumps(dict(os.environ)))

    if behavior == "crash":
        out(
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "contentIndex": 0,
                    "delta": "partial wor",
                },
            }
        )
        time.sleep(0.05)
        os._exit(spec.get("exit_code", 3))
    if behavior in ("hang", "hang_deaf"):
        out(
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "contentIndex": 0,
                    "delta": "thinking...\n",
                },
            }
        )
        if behavior == "hang_deaf":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            while True:
                try:
                    commands.get(timeout=0.5)
                except queue.Empty:
                    pass
        abort = wait_for(3600, lambda c: c.get("type") == "abort")
        if abort:
            respond(abort)
            settle(assistant_message("", usage, "aborted"), aborted=True)
        return
    if behavior in ("ui_confirm", "ui_select"):
        method = "confirm" if behavior == "ui_confirm" else "select"
        req = {
            "type": "extension_ui_request",
            "id": "ui-1",
            "method": method,
            "title": "Allow?",
            "options": ["a", "b"],
        }
        out(req)
        reply = wait_for(5, lambda c: c.get("type") == "extension_ui_response")
        text = "ui-reply: " + json.dumps(reply)
        out(
            {
                "type": "extension_ui_request",
                "id": "n-1",
                "method": "notify",
                "message": "hello",
                "notifyType": "info",
            }
        )

    out(
        {
            "type": "message_update",
            "usage": usage,
            "assistantMessageEvent": {
                "type": "text_delta",
                "contentIndex": 0,
                "delta": text + "\n",
            },
        }
    )
    for key in totals:
        totals[key] += usage.get(key, 0)
    msg_count[0] += 1
    with open(session_file, "a") as fh:
        fh.write(json.dumps({"type": "message", "role": "assistant", "content": text}) + "\n")
    if behavior == "error":
        settle(
            assistant_message(
                text,
                usage,
                spec.get("stop_reason", "error"),
                spec.get("error_message", "provider exploded"),
            )
        )
    else:
        settle(assistant_message(text, usage, spec.get("stop_reason", "stop")))


while True:
    cmd = commands.get()
    if cmd is None:
        sys.exit(0)
    kind = cmd.get("type")
    if kind == "prompt":
        run_turn(cmd)
    elif kind == "get_last_assistant_text":
        respond(cmd, {"text": last_text[0]})
    elif kind == "get_session_stats":
        respond(
            cmd,
            {"sessionFile": str(session_file), "tokens": {**totals, "total": sum(totals.values())}},
        )
    elif kind == "get_state":
        respond(
            cmd, {"sessionFile": str(session_file), "sessionId": session_id, "isStreaming": False}
        )
    elif kind == "abort":
        respond(cmd)
    elif kind == "extension_ui_response":
        pass
    else:
        respond(cmd, success=False, error=f"unknown command {kind}")
