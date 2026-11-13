#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.9"]
# ///
import asyncio
import json
import os
import re
import shlex
import subprocess
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from aiohttp import web

CLAUDE_DIR = Path.home() / ".claude"
CACHE_DIR = Path.home() / ".cache" / "cc-dash"
SUMMARY_FILE = CACHE_DIR / "summaries.json"
HERE = Path(__file__).parent
PORT = int(os.environ.get("CC_DASH_PORT", 8787))
AUTO_SUMMARY_DAYS = 7
MAX_PARALLEL_SUMMARIES = 3
BUSY_RESUMMARIZE_SECS = 300


class Transcript:
    """Incrementally parsed transcript; only newly appended bytes are read on each refresh."""

    def __init__(self, path: Path):
        self.path = path
        self.id = path.stem
        self.offset = 0
        self.first_cwd = self.cwd = self.branch = None
        self.ai_title = self.custom_title = self.last_prompt = None
        self.first_ts = self.last_ts = None
        self.prs = {}
        self.messages = deque(maxlen=40)
        self.n_messages = 0

    def refresh(self):
        size = self.path.stat().st_size
        if size < self.offset:
            self.__init__(self.path)
        if size == self.offset:
            return
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read()
        end = data.rfind(b"\n") + 1
        self.offset += end
        for line in data[:end].splitlines():
            try:
                self._ingest(json.loads(line))
            except (json.JSONDecodeError, AttributeError, TypeError, KeyError):
                pass

    def _ingest(self, e):
        t = e.get("type")
        if t == "ai-title":
            self.ai_title = e.get("aiTitle") or self.ai_title
        elif t == "custom-title":
            self.custom_title = e.get("customTitle") or self.custom_title
        elif t == "last-prompt":
            self.last_prompt = e.get("lastPrompt") or self.last_prompt
        elif t == "pr-link":
            self.prs[e["prUrl"]] = e["prNumber"]
        elif t in ("user", "assistant") and not e.get("isSidechain"):
            if e.get("cwd"):
                self.first_cwd = self.first_cwd or e["cwd"]
                self.cwd = e["cwd"]
            self.branch = e.get("gitBranch") or self.branch
            ts = e.get("timestamp")
            self.first_ts = self.first_ts or ts
            self.last_ts = ts or self.last_ts
            text = _message_text(t, e)
            if text:
                self.messages.append((t, text))
                self.n_messages += 1


def _message_text(role, e):
    if e.get("isMeta"):
        return None
    content = e.get("message", {}).get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    parts = []
    for block in content or []:
        if block.get("type") == "text":
            text = block["text"].strip()
            if role == "user" and text.startswith(("<command-", "<local-command", "<system-reminder", "<task-notification")):
                continue
            parts.append(text[:1500])
        elif block.get("type") == "tool_use" and role == "assistant":
            inp = block.get("input", {})
            hint = inp.get("description") or inp.get("file_path") or inp.get("command") or ""
            parts.append(f"[{block['name']}: {str(hint)[:120]}]")
    return "\n".join(parts) or None


transcripts: dict[str, Transcript] = {}
summaries: dict = json.loads(SUMMARY_FILE.read_text()) if SUMMARY_FILE.exists() else {}
summarizing: set[str] = set()


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def live_sessions():
    live = {}
    for f in (CLAUDE_DIR / "sessions").glob("*.json"):
        try:
            s = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if s.get("cwd", "").startswith("gc://") or not pid_alive(s["pid"]):
            continue
        live[s["sessionId"]] = s
    return live


def refresh_index():
    for path in (CLAUDE_DIR / "projects").glob("*/*.jsonl"):
        t = transcripts.get(path.stem) or transcripts.setdefault(path.stem, Transcript(path))
        try:
            t.refresh()
        except OSError:
            pass


def session_rows():
    refresh_index()
    live = live_sessions()
    rows = []
    for t in transcripts.values():
        if not t.n_messages:
            continue
        s = live.get(t.id)
        rows.append({
            "id": t.id,
            "title": t.custom_title or t.ai_title or (t.last_prompt or "")[:80] or "(untitled)",
            "cwd": t.cwd,
            "resume_cwd": t.first_cwd,
            "resume_cwd_exists": bool(t.first_cwd and os.path.isdir(t.first_cwd)),
            "branch": t.branch,
            "prs": [{"url": u, "number": n} for u, n in t.prs.items()],
            "last_prompt": t.last_prompt,
            "first_ts": t.first_ts,
            "last_ts": t.last_ts,
            "live": bool(s),
            "status": s["status"] if s else "closed",
            "pid": s["pid"] if s else None,
            "name": s.get("name") if s else None,
            "summary": summaries.get(t.id, {}).get("summary"),
            "summary_stale": summaries.get(t.id, {}).get("key") != t.last_ts,
            "summarizing": t.id in summarizing,
            "resume_cmd": f"cd {shlex.quote(t.first_cwd or '~')} && claude --resume {t.id}",
        })
    rows.sort(key=lambda r: r["last_ts"] or "", reverse=True)
    return rows


SUMMARY_PROMPT = """You are summarizing a Claude Code terminal session so its owner, who juggles many sessions, can instantly see what it is about and whether it needs them.
Working directory: {cwd}
Git branch: {branch}

Recent conversation (oldest first, may be truncated):
{convo}

Respond with ONLY a JSON object, no prose, with keys:
"goal": one short sentence, the overall task of the session.
"state": one or two short sentences, where things currently stand (what was done, what's in progress).
"waiting_on_user": true if the last assistant turn ends asking the user a question, needing a decision/approval, or reporting finished work awaiting review; false if it is mid-work or the session is simply done with nothing pending.
"waiting_reason": short phrase, what exactly the user needs to do (empty if not waiting).
"next": short phrase, the most likely next step."""


async def summarize(t: Transcript):
    summarizing.add(t.id)
    key = t.last_ts
    convo = "\n\n".join(f"{role.upper()}: {text}" for role, text in list(t.messages)[-30:])
    prompt = SUMMARY_PROMPT.format(cwd=t.cwd, branch=t.branch, convo=convo[-40000:])
    workdir = CACHE_DIR / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "--model", "haiku", "--tools", "", "--no-session-persistence",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            cwd=workdir,
        )
        out, _ = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=120)
        match = re.search(r"\{.*\}", out.decode(), re.S)
        summaries[t.id] = {"key": key, "at": time.time(), "summary": json.loads(match.group(0))}
        SUMMARY_FILE.write_text(json.dumps(summaries))
    except Exception as exc:
        print(f"summary failed for {t.id}: {exc!r}")
    finally:
        summarizing.discard(t.id)


async def summary_loop():
    sem = asyncio.Semaphore(MAX_PARALLEL_SUMMARIES)

    async def run(t):
        async with sem:
            await summarize(t)

    while True:
        try:
            rows = session_rows()
            cutoff = time.time() - AUTO_SUMMARY_DAYS * 86400
            for r in rows:
                if not r["summary_stale"] or r["summarizing"] or not r["last_ts"]:
                    continue
                if datetime.fromisoformat(r["last_ts"]).timestamp() < cutoff:
                    continue
                prev = summaries.get(r["id"], {})
                if r["status"] == "busy" and time.time() - prev.get("at", 0) < BUSY_RESUMMARIZE_SECS:
                    continue
                summarizing.add(r["id"])
                asyncio.create_task(run(transcripts[r["id"]]))
        except Exception as exc:
            print(f"summary loop error: {exc!r}")
        await asyncio.sleep(10)


async def api_sessions(request):
    return web.json_response(session_rows())


async def api_summarize(request):
    t = transcripts.get(request.match_info["id"])
    if not t:
        raise web.HTTPNotFound()
    if t.id not in summarizing:
        summarizing.add(t.id)
        asyncio.create_task(summarize(t))
    return web.json_response({"ok": True})


async def api_resume(request):
    t = transcripts.get(request.match_info["id"])
    if not t or not t.first_cwd or not os.path.isdir(t.first_cwd):
        return web.json_response({"ok": False, "error": "working directory no longer exists"}, status=400)
    cmd = f"claude --resume {t.id}; exec $SHELL"
    subprocess.Popen(
        ["terminator", "--new-tab", "--working-directory", t.first_cwd, "-x", os.environ.get("SHELL", "zsh"), "-ic", cmd],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return web.json_response({"ok": True})


async def index(request):
    return web.FileResponse(HERE / "index.html")


async def on_startup(app):
    app["summary_task"] = asyncio.create_task(summary_loop())


def main():
    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/api/sessions", api_sessions),
        web.post("/api/summarize/{id}", api_summarize),
        web.post("/api/resume/{id}", api_resume),
    ])
    app.on_startup.append(on_startup)
    print(f"cc-dash on http://localhost:{PORT}")
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
