#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.9"]
# ///
import asyncio
import fcntl
import json
import os
import re
import shlex
import signal
import struct
import subprocess
import termios
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

from aiohttp import web

CLAUDE_DIR = Path.home() / ".claude"
CACHE_DIR = Path.home() / ".cache" / "cc-dash"
SUMMARY_FILE = CACHE_DIR / "summaries.json"
STATE_FILE = CACHE_DIR / "state.json"
HERE = Path(__file__).parent
TMUX = ["tmux", "-L", "cc-dash", "-f", str(HERE / "tmux.conf")]
SHELL = os.environ.get("SHELL", "/bin/zsh")
GC_DIR = Path.home() / ".gc"
GC_BIN = GC_DIR / "bin" / "gc"
PORT = int(os.environ.get("CC_DASH_PORT", 8787))
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
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
state: dict = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"groups": [], "stars": [], "notes": {}}
launched: dict[str, str] = {}  # new session id -> cwd, until its transcript has messages


def tmux_name(sid):
    return f"cc-{sid[:8]}"


def tmux_sessions():
    out = subprocess.run(TMUX + ["list-sessions", "-F", "#{session_name}"], capture_output=True, text=True)
    return set(out.stdout.split())


def tmux_start(sid, cwd, claude_args):
    subprocess.run(TMUX + [
        "new-session", "-d", "-s", tmux_name(sid), "-c", cwd, "-x", "200", "-y", "50",
        "-e", "COLORTERM=truecolor", SHELL, "-ic", f"claude {claude_args}",
    ], check=True)


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


def peer_sessions():
    """Coworkers' shared sessions, which the intern-groupchat bridge registers as gc:// entries."""
    peers = []
    for f in (CLAUDE_DIR / "sessions").glob("*.json"):
        try:
            s = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if s.get("cwd", "").startswith("gc://"):
            peers.append(s)
    return peers


def gc_shared():
    try:
        return json.loads((GC_DIR / "shared.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def row(**kw):
    return {
        "title": "", "cwd": None, "resume_cwd": None, "resume_cwd_exists": False, "branch": None, "prs": [],
        "last_prompt": None, "first_ts": None, "last_ts": None, "live": False, "status": "closed", "pid": None,
        "name": None, "summary": None, "summary_stale": False, "summarizing": False, "tmux": None,
        "shared": None, "peer": None, "fresh": False, "resume_cmd": "", **kw,
    }


def refresh_index():
    for path in (CLAUDE_DIR / "projects").glob("*/*.jsonl"):
        t = transcripts.get(path.stem) or transcripts.setdefault(path.stem, Transcript(path))
        try:
            t.refresh()
        except OSError:
            pass
    for sid in [sid for sid, t in transcripts.items() if not t.path.exists()]:
        del transcripts[sid]


def session_rows():
    refresh_index()
    live = live_sessions()
    in_tmux = tmux_sessions()
    shared = gc_shared()
    rows = []
    # sessions nobody has written to yet have no transcript; find them via the registry so they survive server restarts
    used = {sid for sid, t in transcripts.items() if t.n_messages}
    for sid in list(launched):
        if sid in used or tmux_name(sid) not in in_tmux:
            launched.pop(sid)
    fresh = {sid: s["cwd"] for sid, s in live.items() if sid not in used and s.get("kind") == "interactive"}
    for sid, cwd in {**fresh, **launched}.items():
        s = live.get(sid, {})
        tmux = tmux_name(sid) if tmux_name(sid) in in_tmux else None
        rows.append(row(
            id=sid, title="New session", fresh=True, cwd=cwd, resume_cwd=cwd, resume_cwd_exists=True, live=True,
            status=s.get("status", "starting"), pid=s.get("pid"), tmux=tmux, shared=shared.get(sid),
            resume_cmd=f"tmux -L cc-dash attach -t {tmux}" if tmux else "",
        ))
    for s in peer_sessions():
        owner, _, title = s["name"].partition(":")
        rows.append(row(id=f"peer:{s['name']}", peer=s["name"], title=title, cwd=owner, live=True, status=s.get("status", "idle")))
    for t in transcripts.values():
        if not t.n_messages:
            continue
        s = live.get(t.id)
        tmux = tmux_name(t.id) if tmux_name(t.id) in in_tmux else None
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
            "tmux": tmux,
            "shared": shared.get(t.id),
            "peer": None,
            "resume_cmd": f"tmux -L cc-dash attach -t {tmux}" if tmux
            else f"cd {shlex.quote(t.first_cwd or '~')} && claude --resume {t.id}",
        })
    rows.sort(key=lambda r: r["last_ts"] or "9", reverse=True)
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
    """Start a closed session inside tmux (default) or open it in a Terminator tab."""
    sid = request.match_info["id"]
    target = (await request.json()).get("target", "tmux") if request.can_read_body else "tmux"
    name = tmux_name(sid)
    hosted = name in tmux_sessions()
    t = transcripts.get(sid)
    if not hosted and sid in live_sessions():
        return web.json_response({"ok": False, "error": "already running in another terminal"}, status=409)
    if not hosted and (not t or not t.first_cwd or not os.path.isdir(t.first_cwd)):
        return web.json_response({"ok": False, "error": "working directory no longer exists"}, status=400)
    if target == "terminator":
        cmd = TMUX + ["attach", "-t", name] if hosted else [SHELL, "-ic", f"claude --resume {sid}; exec {SHELL}"]
        subprocess.Popen(["terminator", "--new-tab", "--working-directory", t.first_cwd if t else str(Path.home()), "-x", *cmd],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif not hosted:
        tmux_start(sid, t.first_cwd, f"--resume {sid}")
    return web.json_response({"ok": True})


async def api_discard(request):
    """End a dashboard session that was never written to, so nothing is lost."""
    sid = request.match_info["id"]
    refresh_index()
    if transcripts.get(sid) and transcripts[sid].n_messages:
        return web.json_response({"ok": False, "error": "session already has a conversation"}, status=409)
    subprocess.run(TMUX + ["kill-session", "-t", tmux_name(sid)], capture_output=True)
    launched.pop(sid, None)
    return web.json_response({"ok": True})


async def api_new(request):
    cwd = os.path.expanduser((await request.json())["cwd"])
    if not os.path.isdir(cwd):
        return web.json_response({"ok": False, "error": "no such directory"}, status=400)
    sid = str(uuid.uuid4())
    tmux_start(sid, cwd, f"--session-id {sid}")
    launched[sid] = cwd
    return web.json_response({"ok": True, "id": sid})


async def api_share(request):
    """Share or unshare a running session through intern-groupchat's `gc` CLI."""
    s = live_sessions().get(request.match_info["id"])
    if not s:
        return web.json_response({"ok": False, "output": "only running sessions can be shared"}, status=400)
    body = await request.json()
    if body.get("unshare"):
        args = ["unshare", "--pid", str(s["pid"])]
    else:
        args = ["share", "--pid", str(s["pid"]), *(["--with-history"] if body.get("history") else []), *body["with"].split()]
    proc = await asyncio.create_subprocess_exec(str(GC_BIN), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return web.json_response({"ok": proc.returncode == 0, "output": out.decode().strip()})


async def api_gc(request):
    try:
        port = json.loads((GC_DIR / "config.json").read_text()).get("web_port", 7779)
    except (OSError, json.JSONDecodeError):
        port = 7779
    return web.json_response({"installed": GC_BIN.exists(), "web": f"http://127.0.0.1:{port}"})


@web.middleware
async def guard(request, handler):
    """The API can start shells, so only this machine's own page may use it: blocks DNS rebinding and cross-site requests."""
    origin = request.headers.get("Origin")
    if request.host not in ALLOWED_HOSTS or (origin and origin.split("://", 1)[-1] not in ALLOWED_HOSTS):
        raise web.HTTPForbidden()
    if request.method != "GET" and request.headers.get("X-CC-Dash") != "1":
        raise web.HTTPForbidden()
    return await handler(request)


async def api_get_state(request):
    return web.json_response(state)


async def api_put_state(request):
    state.update(await request.json())
    STATE_FILE.write_text(json.dumps(state))
    return web.json_response({"ok": True})


async def ws_term(request):
    """Bridge a websocket to `tmux attach` running in a fresh pty. Closing it only detaches."""
    name = request.match_info["name"]
    if name not in tmux_sessions():
        raise web.HTTPNotFound()
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)
    pid, fd = os.forkpty()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.execvp("tmux", TMUX + ["attach", "-t", name])
    loop = asyncio.get_running_loop()
    out = asyncio.Queue()

    def on_output():
        try:
            out.put_nowait(os.read(fd, 65536))
        except OSError:
            loop.remove_reader(fd)
            out.put_nowait(None)

    async def pump():
        while (data := await out.get()) is not None:
            await ws.send_bytes(data)
        await ws.close()

    loop.add_reader(fd, on_output)
    pump_task = asyncio.create_task(pump())
    try:
        async for msg in ws:
            m = json.loads(msg.data)
            if "i" in m:
                os.write(fd, m["i"].encode())
            elif "r" in m:
                cols, rows = m["r"]
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    finally:
        loop.remove_reader(fd)
        pump_task.cancel()
        os.kill(pid, signal.SIGHUP)
        os.close(fd)
        await loop.run_in_executor(None, os.waitpid, pid, 0)
    return ws


async def index(request):
    return web.FileResponse(HERE / "index.html")


async def on_startup(app):
    app["summary_task"] = asyncio.create_task(summary_loop())


def main():
    app = web.Application(middlewares=[guard])
    app.add_routes([
        web.get("/", index),
        web.get("/api/sessions", api_sessions),
        web.post("/api/summarize/{id}", api_summarize),
        web.post("/api/resume/{id}", api_resume),
        web.post("/api/new", api_new),
        web.post("/api/discard/{id}", api_discard),
        web.get("/api/state", api_get_state),
        web.get("/api/gc", api_gc),
        web.post("/api/share/{id}", api_share),
        web.put("/api/state", api_put_state),
        web.get("/ws/term/{name}", ws_term),
    ])
    app.on_startup.append(on_startup)
    print(f"cc-dash on http://localhost:{PORT}")
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
