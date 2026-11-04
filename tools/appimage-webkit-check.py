#!/usr/bin/env python3
"""Check that an AppImage's own WebKitGTK works (HbbTV): run with the
AppImage's Python and environment (see .github/workflows/appimage.yml).

Loads a page in a WebView, waits for its script, and checks that the web
process came from the AppImage, not from the system. Exit status 0 = fine.
"""

import os
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import GLib, Gtk, WebKit  # noqa: E402


def main():
    here = os.path.realpath(os.path.join(os.path.dirname(sys.executable), "..", ".."))
    win = Gtk.Window()
    view = WebKit.WebView()
    win.set_child(view)
    win.present()
    view.load_html("<script>document.title = 'ok:' + typeof MediaSource;</script>", "http://localhost/")
    ctx, end = GLib.MainContext.default(), time.time() + 60
    while time.time() < end and not (view.get_title() or "").startswith("ok"):
        ctx.iteration(False)
        time.sleep(0.01)
    title = view.get_title() or ""
    exes = set()
    for pid in subprocess.run(["pgrep", "-f", "WebKitWebProcess"], capture_output=True,
                              text=True).stdout.split():
        try:
            exes.add(os.readlink(f"/proc/{pid}/exe"))
        except OSError:
            pass
    print("title:", title)
    print("web processes:", sorted(exes))
    ours = [e for e in exes if e.startswith(here)]
    if title != "ok:function" or not ours:
        sys.exit("WebKit in the AppImage does not work")


if __name__ == "__main__":
    main()
