# Setting up cc-dash (instructions for an agent)

You are installing cc-dash for the user on their own Linux machine. Follow the steps in
order, run each verification, and stop and report to the user if one fails. Do not change
anything outside the paths named here.

## 1. Check prerequisites

```sh
python3 --version          # needs >= 3.11
uv --version               # https://docs.astral.sh/uv/ ; the server runs via `uv run --script`
tmux -V                    # holds the Claude sessions; e.g. `sudo apt install tmux`
claude --version           # Claude Code, logged in (summaries call `claude -p --model haiku`)
systemctl --user status >/dev/null && echo ok   # systemd user session
```

Optional, each only enables one feature:

| Check | Enables |
|---|---|
| `python3 -c "import gi; gi.require_version('WebKit2','4.1')"` (Ubuntu: `gir1.2-webkit2-4.1`) | `app.py`, the standalone window |
| `which terminator` | "Resume / open in Terminator" buttons |
| `~/.gc/bin/gc status` ([intern-groupchat](https://github.com/olivefi/intern-groupchat)) | sharing badges, peers, chat panes |

Ask the user before installing anything with `sudo`.

## 2. Get the code

```sh
git clone https://github.com/olivefi/cc-dash.git ~/cc-dash
```

The service file below assumes `~/cc-dash`; if you clone elsewhere, adjust its paths.

## 3. Install the systemd user service

```sh
mkdir -p ~/.config/systemd/user
cp ~/cc-dash/contrib/cc-dash.service ~/.config/systemd/user/
getent passwd "$USER" | cut -d: -f7     # the user's login shell
```

Edit `~/.config/systemd/user/cc-dash.service`:
- set `Environment=SHELL=` to that login shell
- make sure `PATH=` contains the directories of `uv` and `claude` (`dirname $(which uv claude)`)

Keep `KillMode=process`: the tmux server that holds the user's Claude sessions is started by
this service, and any other kill mode ends every session on restart.

```sh
systemctl --user daemon-reload
systemctl --user enable --now cc-dash
```

## 4. Verify

```sh
systemctl --user is-active cc-dash                 # active
curl -s localhost:8787/api/gc                      # JSON, e.g. {"installed": false, ...}
curl -s localhost:8787/api/sessions | head -c 300  # JSON list of the user's sessions
journalctl --user -u cc-dash -n 20 --no-pager      # no tracebacks
```

The first start downloads `aiohttp` into uv's cache and can take ~10 s.

## 5. Job tags (optional, ask the user)

Lets sessions mark outstanding GPU runs / cluster jobs so they show up in the sidebar's Jobs
section. Three pieces, all pointing into the clone:

```sh
ln -s ~/cc-dash/cc-job ~/.local/bin/cc-job                  # the command sessions run
mkdir -p ~/.claude/skills && ln -s ~/cc-dash/skills/cc-job ~/.claude/skills/cc-job   # tells sessions when to run it
cc-job --help                                               # verify (needs ~/.local/bin on PATH)
```

Optionally auto-tag `sbatch` submissions: add this to the `hooks` of `~/.claude/settings.json`
(merge with existing hooks; keep the file valid JSON):

```json
"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "~/cc-dash/hooks/sbatch-tag.py"}]}]
```

Verify the hook: `printf '%s' '{"tool_name":"Bash","session_id":"t","tool_input":{"command":"sbatch x"},"tool_response":"Submitted batch job 1"}' | ~/cc-dash/hooks/sbatch-tag.py && cc-job --session t ls && cc-job --session t done`

## 6. Tell the user

- Open http://localhost:8787, or run `~/cc-dash/app.py` for a standalone window (recommended:
  browser tabs swallow Ctrl+W / Ctrl+T / Ctrl+N).
- It starts automatically at login.
- Point them to the "Using it" section of the README.

## Updating

```sh
cd ~/cc-dash && git pull && systemctl --user restart cc-dash
```

Restarts take a few seconds and do not affect running sessions; reload the page afterwards.

## Never

- Bind the server to anything but 127.0.0.1 or proxy it: it can start shells and type into them.
- Kill the tmux server (`tmux -L cc-dash kill-server`) without asking: it ends all sessions.
