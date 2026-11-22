# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = [
#   "flask>=3", "pillow>=10", "pillow-heif>=0.16",
#   "opencv-python-headless>=4.8", "numpy>=1.26",
#   "pyobjc-framework-Vision>=10", "rumps>=0.4",
# ]
# ///
"""PlayBill Display menu bar app: runs the library + Kindle sync server in the background.

Launched by "PlayBill Display.app" (see build_app.sh), or directly:  uv run menubar.py
"""
import os
import plistlib
import subprocess
import sys
import threading
import time
import webbrowser

import rumps
from werkzeug.serving import make_server

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as server  # noqa: E402  (the Flask app in app.py)

URL = f"http://localhost:{server.PORT}"
AGENT_LABEL = "com.playbilldisplay.menubar"
AGENT_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{AGENT_LABEL}.plist")
APP_BUNDLE = os.environ.get("PLAYBILL_APP_BUNDLE", "")  # set by the .app launcher


def ago(t: float) -> str:
    s = int(time.time() - t)
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60} min ago"
    return f"{s // 3600} h ago"


class PlayBillApp(rumps.App):
    def __init__(self):
        super().__init__("PlayBill Display", title="🎭", quit_button=None)
        try:  # menu bar only: no Dock icon (we run as a python process inside the .app)
            from AppKit import NSApplication
            NSApplication.sharedApplication().setActivationPolicy_(1)  # Accessory
        except Exception:
            pass
        self.status = rumps.MenuItem("Kindle: not heard from yet")
        self.showing = rumps.MenuItem("")
        self.next_item = rumps.MenuItem("Show Next Playbill on Kindle", callback=self.show_next)
        self.unpin_item = rumps.MenuItem("Unpin (Resume Rotation)", callback=self.unpin)
        self.login_item = rumps.MenuItem("Start at Login", callback=self.toggle_login)
        self.login_item.state = os.path.exists(AGENT_PATH)
        self.menu = [
            rumps.MenuItem("Open Library…", callback=self.open_library),
            None,
            self.status,
            self.showing,
            self.next_item,
            self.unpin_item,
            None,
            rumps.MenuItem(f"Kindle syncs from {server.server_url()}"),
            self.login_item,
            None,
            rumps.MenuItem("Quit PlayBill Display", callback=self.quit),
        ]
        self.refresh(None)
        rumps.Timer(self.refresh, 10).start()

    # ---- status ----
    def refresh(self, _):
        seen = server._kindle_seen
        items = server.load_index()
        by_out = {it.get("out"): it for it in items}
        if seen["at"]:
            self.status.title = f"Kindle checked in {ago(seen['at'])}"
            it = by_out.get(seen["showing"])
            self.showing.title = f"Showing: {it['info'].get('show') or it['title'] if it else '—'}"
        else:
            self.status.title = "Kindle: not heard from yet"
            self.showing.title = f"{len(items)} Playbills in library"
        d = server.load_display()
        self.unpin_item.set_callback(self.unpin if d.get("pin") else None)
        self.next_item.set_callback(self.show_next if items else None)

    # ---- actions ----
    def open_library(self, _):
        webbrowser.open(URL)

    def show_next(self, _):
        items = server.load_index()
        if not items:
            return
        outs = [it.get("out") for it in items]
        cur = server._kindle_seen.get("showing")
        i = (outs.index(cur) + 1) % len(items) if cur in outs else 0
        with server._lock:
            server.request_display(items[i]["id"])
        self.showing.title = f"Sending: {items[i]['title']} (≤1 min)"

    def unpin(self, _):
        with server._lock:
            server.request_display(None)
        self.refresh(None)

    def toggle_login(self, sender):
        if sender.state:
            subprocess.run(["launchctl", "unload", AGENT_PATH], capture_output=True)
            try:
                os.remove(AGENT_PATH)
            except FileNotFoundError:
                pass
            sender.state = False
            return
        if not APP_BUNDLE:
            rumps.alert("Start at Login", "Launch PlayBill Display from the app (not the terminal) to set this up.")
            return
        os.makedirs(os.path.dirname(AGENT_PATH), exist_ok=True)
        with open(AGENT_PATH, "wb") as f:
            plistlib.dump({
                "Label": AGENT_LABEL,
                "ProgramArguments": ["/usr/bin/open", "-g", APP_BUNDLE, "--args", "--no-browser"],
                "RunAtLoad": True,
            }, f)
        sender.state = True

    def quit(self, _):
        rumps.quit_application()


def main():
    server.prepare()
    try:
        httpd = make_server("0.0.0.0", server.PORT, server.app, threaded=True)
    except OSError as e:
        # Most likely it's already running (another copy, or `uv run app.py` in a terminal).
        rumps.alert("PlayBill Display",
                    f"Can't start: port {server.PORT} is in use ({e.strerror}).\n\n"
                    "PlayBill Display may already be running. Look for 🎭 in the menu bar.")
        webbrowser.open(URL)
        return
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"PlayBill Display serving on {URL} (Kindle uses {server.server_url()})", flush=True)
    if "--no-browser" not in sys.argv:
        webbrowser.open(URL)
    PlayBillApp().run()


if __name__ == "__main__":
    main()
