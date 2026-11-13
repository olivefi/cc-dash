# cc-dash

A local dashboard for keeping track of many Claude Code sessions: what each one is doing,
which ones need you, and all of them in Terminator-style split views in one window.

- **Overview:** every session on the machine, open or closed, with a short Haiku summary
  (goal, state, next step) and a flag when one is waiting on you.
- **Find them again:** search across titles, summaries, notes, paths and branches; star
  sessions; leave notes on them; resume any closed session with one click.
- **Work in it:** sessions run inside the dashboard as real terminals, arranged by drag and
  drop into groups of split panes. They live in tmux, so closing the window or restarting
  the server never interrupts them.
- **Optional [intern-groupchat](https://github.com/olivefi/intern-groupchat) integration:**
  see which sessions you share, share or unshare them, and open coworkers' shared sessions
  as chat panes.

Linux only (tmux, ptys, systemd). Setup: [SETUP.md](SETUP.md), written so you can hand it to a
Claude Code agent ("set up cc-dash following SETUP.md").

## Using it

Open http://localhost:8787, or `~/cc-dash/app.py` for its own window. The window is
recommended: in a browser tab, Ctrl+W / Ctrl+T / Ctrl+N close or open tabs instead of
reaching Claude.

### Sidebar

- Sections: **Starred**, **Open** (sorted: needs you → working → idle), **Peers** (if
  intern-groupchat is installed), **Closed** in the last 14 days, and **Older**.
- `/` focuses the search box, which matches titles, summaries, notes, paths, branches and ids.
- Click a session for details: goal / state / next step, PR links, a notes box, sharing,
  and buttons to add it to the current group, open it in Terminator, copy its resume
  command, (re)summarize it or end it.
- ☆ stars a session. A note's first line shows under the session.
- **+ New** starts a session in any directory; ☆ next to **Start** saves that directory as a
  favorite, which then appears as a one-click button under the search box (hover → ✕ to remove).
- ⚙ holds the settings, e.g. turning automatic summaries off.

### Groups and panes

- Tabs at the top are groups. **+ group** adds one, double-click renames, ✕ (click twice)
  deletes; deleting a group never ends sessions.
- Drag a session from the sidebar into the workspace. Drop it on a pane's left, right, top or
  bottom edge to split that pane; a highlight shows where it lands. Drop it on a tab to add it
  to that group.
- Drag a pane by its header to move it, onto another tab to move it to that group, or back onto
  the sidebar to take it out of the group.
- Drag the gaps between panes to resize them.

Pane header buttons:

| | |
|---|---|
| ☆ | star |
| ✎ | show / hide the note box in the pane's top-right corner (edits sync with the sidebar) |
| ⊟ / Ctrl+Shift+O | new Claude below, in the same folder |
| ◫ / Ctrl+Shift+E | new Claude to the right, in the same folder |
| ⤢ | maximize / restore |
| ✕ | close: ends the session if it is idle (resume it any time, history included); a session that is still working keeps running in the background |

In a terminal, Shift+Enter inserts a newline.

A closed session in a group shows its summary and **Resume here**. A session running in an
ordinary terminal can't be embedded; exit it there and resume it here.

### From a terminal

Dashboard sessions are ordinary tmux sessions on their own socket:

```sh
tmux -L cc-dash ls
tmux -L cc-dash attach -t cc-<first 8 chars of the session id>
```

### Summaries

Sessions active in the last 7 days are summarized with Haiku whenever they change (at most
every 5 minutes while busy). Each summary is a cheap `claude -p` call on your account. Turn
them off under ⚙; the per-session **Summarize** button still works.

## Files

| | |
|---|---|
| `server.py` | aiohttp server: session index, summaries, tmux, terminal websockets |
| `index.html` | the whole UI |
| `app.py` | standalone GTK WebKit window |
| `tmux.conf` | config of the dashboard's tmux server |
| `contrib/cc-dash.service` | systemd user service |
| `~/.cache/cc-dash/` | state (groups, stars, notes, favorites, settings) and summaries |

The server only listens on 127.0.0.1 and refuses requests from other sites, since it can
start shells. Don't expose it.
