#!/usr/bin/env python3
"""Claude Code SessionEnd telemetry adapter for the existing reporting API.

Only sends metadata and aggregate token counts, never transcript contents.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.request import Request, urlopen

API_URL = os.environ.get("CURSOR_METRICS_URL", "http://localhost:8000").rstrip("/")
STATE_DIR = Path(os.environ.get("CLAUDE_METRICS_STATE_DIR", str(Path.home() / ".claude" / "reporting-state")))
COUNTERS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
COMMAND_TO_SKILL = {"specify": "spec-creation", "design": "design-creation", "tasks": "task-creation", "implement": "implementation", "review": "code-review"}


def _rows(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        yield row
                except (ValueError, TypeError):
                    continue
    except (OSError, UnicodeError):
        return


def _transcripts(primary, session_id):
    primary = Path(primary).expanduser().resolve()
    if not primary.is_file() or primary.suffix != ".jsonl":
        return []
    paths = [primary]
    subagent_root = primary.parent / session_id / "subagents"
    if subagent_root.is_dir():
        paths += sorted(p for p in subagent_root.rglob("*.jsonl") if p.is_file())
    return paths


def _command(primary):
    import re
    for row in _rows(primary):
        if row.get("type") != "user":
            continue
        content = row.get("message", {}).get("content", "")
        if isinstance(content, list):
            content = " ".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
        if not isinstance(content, str):
            continue
        match = re.search(r"(?:^|\s)/([a-z][a-z0-9-]*)\b", content)
        if match:
            return match.group(1)
    return None


def _usage(row):
    if row.get("type") != "assistant":
        return None
    message = row.get("message", {})
    if not isinstance(message, dict):
        return None
    model = message.get("model")
    usage = message.get("usage")
    if not isinstance(model, str) or not model or model.startswith("<") or not isinstance(usage, dict):
        return None
    mid = message.get("id") or row.get("requestId")
    if not isinstance(mid, str) or not mid:
        return None
    counts = {k: max(0, v) if type(v) is int else 0 for k in COUNTERS for v in [usage.get(k)]}
    nested = usage.get("cache_creation")
    if isinstance(nested, dict):
        nested_total = sum(max(0, v) for v in nested.values() if type(v) is int)
        counts["cache_creation_input_tokens"] = max(counts["cache_creation_input_tokens"], nested_total)
    return (model, mid), counts


def summarize(primary, session_id):
    """Retain max usage per counter for repeated assistant message IDs."""
    messages = {}
    for path in _transcripts(primary, session_id):
        for row in _rows(path):
            result = _usage(row)
            if result is None:
                continue
            key, counts = result
            prior = messages.setdefault(key, {k: 0 for k in COUNTERS})
            for counter, value in counts.items():
                prior[counter] = max(prior[counter], value)
    models = {}
    for (model, _), counts in messages.items():
        target = models.setdefault(model, {k: 0 for k in COUNTERS})
        for k, value in counts.items():
            target[k] += value
    return models


def _email():
    try:
        return subprocess.check_output(["git", "config", "user.email"], text=True, stderr=subprocess.DEVNULL, timeout=2).strip() or "unknown@local"
    except (OSError, subprocess.SubprocessError):
        return "unknown@local"


def payloads(event):
    if event.get("hook_event_name") != "SessionEnd":
        return []
    session_id = event.get("session_id")
    transcript = event.get("transcript_path")
    if not isinstance(session_id, str) or not session_id or not isinstance(transcript, str):
        return []
    command = _command(transcript)
    result = []
    for model, counts in sorted(summarize(transcript, session_id).items()):
        # Existing PricingService expects input_tokens inclusive of cache tokens.
        inclusive_input = counts["input_tokens"] + counts["cache_read_input_tokens"] + counts["cache_creation_input_tokens"]
        result.append({
            "event_type": "session_end",
            "conversation_id": session_id,
            "session_id": session_id,
            "generation_id": "claude:" + hashlib.sha256((session_id + "\0" + model).encode()).hexdigest()[:32],
            "model": model,
            "user_email": _email(),
            "status": "completed",
            "input_tokens": inclusive_input,
            "output_tokens": counts["output_tokens"],
            "cache_read_tokens": counts["cache_read_input_tokens"],
            "cache_write_tokens": counts["cache_creation_input_tokens"],
            "workspace": event.get("cwd") if isinstance(event.get("cwd"), str) else None,
            "command_name": command,
            "skill_name": COMMAND_TO_SKILL.get(command),
        })
    return result


def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, TypeError):
        return
    if not isinstance(event, dict):
        return
    for payload in payloads(event):
        marker = STATE_DIR / (payload["generation_id"] + ".sent")
        if marker.exists():
            continue
        request = Request(API_URL + "/api/v1/ingest", data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=4) as response:
                if response.status != 202:
                    continue
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            marker.touch(exist_ok=True)
        except (OSError, ValueError):
            continue


if __name__ == "__main__":
    main()
