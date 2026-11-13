#!/usr/bin/python3
"""Opens the dashboard in its own window, so browser shortcuts (Ctrl+W, Ctrl+T, ...) reach the terminals."""
import os
import socket
import subprocess
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gio, Gtk, WebKit2  # noqa: E402

PORT = int(os.environ.get("CC_DASH_PORT", 8787))
HERE = Path(__file__).resolve().parent


def server_up():
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.3).close()
        return True
    except OSError:
        return False


if not server_up():
    log = Path.home() / ".cache" / "cc-dash" / "server.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([str(HERE / "server.py")], cwd=HERE, start_new_session=True,
                     stdout=open(log, "ab"), stderr=subprocess.STDOUT)
    for _ in range(100):
        if server_up():
            break
        time.sleep(0.2)

win = Gtk.Window(title="Claude Sessions")
win.set_default_size(1700, 1000)
view = WebKit2.WebView()
view.get_settings().set_javascript_can_access_clipboard(True)
view.get_settings().set_enable_developer_extras(True)
view.connect("create", lambda _view, action: Gio.AppInfo.launch_default_for_uri(action.get_request().get_uri(), None) and None)
view.load_uri(f"http://localhost:{PORT}")
win.add(view)
win.connect("destroy", Gtk.main_quit)
win.show_all()
Gtk.main()
