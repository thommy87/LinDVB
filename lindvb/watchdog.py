"""Writes where the program hangs when its main loop stops answering.

A timer on the GTK main loop notes the time every second; a thread checks it.
If the main loop has not run for ``STALL`` seconds, the stacks of all threads
are written to ``~/.cache/lindvb/hang.log`` (once per hang), so a freeze can be
traced even when the program has to be killed afterwards.
"""

import faulthandler
import sys
import threading
import time

from gi.repository import GLib

STALL = 8.0


class Watchdog:
    def __init__(self, path, stall=STALL):
        self.path = path
        self.stall = stall
        self.beat = time.monotonic()
        self.reported = False
        self.file = None
        self._stop = threading.Event()

    def start(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            big = self.path.exists() and self.path.stat().st_size > 1_000_000
            self.file = open(self.path, "w" if big else "a", buffering=1)
        except OSError:
            return False
        try:
            faulthandler.enable(self.file)       # crashes (segfaults) too
        except (RuntimeError, ValueError):
            pass
        GLib.timeout_add_seconds(1, self._heartbeat)
        threading.Thread(target=self._watch, name="lindvb-watchdog", daemon=True).start()
        return True

    def stop(self):
        self._stop.set()

    def _heartbeat(self):
        self.beat = time.monotonic()
        if self.reported:
            self.reported = False
            self._write(f"main loop running again at {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        return not self._stop.is_set()

    def _write(self, text):
        try:
            self.file.write(text)
            self.file.flush()
        except (OSError, ValueError):
            pass

    def _watch(self):
        while not self._stop.wait(1.0):
            stalled = time.monotonic() - self.beat
            if stalled > self.stall and not self.reported:
                self.reported = True
                self._write(f"=== LinDVB not responding for {stalled:.0f} s at "
                            f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                try:
                    faulthandler.dump_traceback(self.file, all_threads=True)
                except (OSError, ValueError):
                    pass
                self._write("\n")
                print(f"LinDVB is not responding; details in {self.path}", file=sys.stderr)


def start(path):
    dog = Watchdog(path)
    return dog if dog.start() else None
