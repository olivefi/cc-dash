# cc-dash

Local dashboard for Claude Code sessions: Haiku summaries, search, stars, notes, and
embedded terminals arranged into drag-and-drop split groups.

- `./app.py` opens it in its own window (starts the server if needed). Recommended, since a
  browser tab swallows Ctrl+W / Ctrl+T.
- `./server.py` runs only the server; then open http://localhost:8787 in any browser.

Sessions started or resumed from the dashboard run in tmux (`tmux -L cc-dash ls`), so they
survive closing the window or restarting the server. Attach from any terminal with
`tmux -L cc-dash attach -t cc-<first 8 chars of session id>`.

State (groups, stars, notes) and summaries live in `~/.cache/cc-dash/`.
