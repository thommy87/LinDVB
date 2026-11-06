# SPDX-License-Identifier: GPL-2.0-or-later
"""Optional long-term log of the reception (one line per channel and minute).

Switched on in the channel analysis (Signal ▸ "Log reception"). While it is on,
every tuned DVB channel adds a line a minute to a CSV file per day under
``~/.local/share/lindvb/reception/``: minimum, mean and maximum of the signal
quality and C/N, the worst bit error rate, new uncorrectable blocks and lock
losses. That is enough to show a weak LNB, rain fade or a cable fault over days.
Nothing is written while the setting is off.
"""

import csv
import time
from pathlib import Path

from .config import DATA_DIR

LOG_DIR = DATA_DIR / "reception"
FIELDS = ["time", "channel", "mux", "quality_min", "quality_avg", "quality_max",
          "cnr_min", "cnr_avg", "cnr_max", "ber_max", "unc", "lock_lost"]
KEEP_DAYS = 31


class ReceptionLog:
    def __init__(self, directory=LOG_DIR, clock=time.time):
        self.dir = Path(directory)
        self.clock = clock
        self._acc = {}                 # channel key -> accumulator

    def feed(self, channel, quality, cnr_db=None, ber=None, unc=None, lock=True):
        """One measurement (about every second) of a tuned channel."""
        now = self.clock()
        minute = int(now // 60)
        a = self._acc.get(channel.key)
        if a is not None and a["minute"] != minute:
            self._write(a)
            a = None
        if a is None:
            a = self._acc[channel.key] = {
                "minute": minute, "name": channel.name, "mux": channel.mux_key, "q": [],
                "cnr": [], "ber": 0.0, "unc0": unc, "unc": unc, "lost": 0, "lock": lock}
        a["q"].append(quality)
        if cnr_db is not None:
            a["cnr"].append(cnr_db)
        if ber is not None:
            a["ber"] = max(a["ber"], ber)
        if unc is not None:
            if a["unc0"] is None:
                a["unc0"] = unc
            a["unc"] = unc
        if a["lock"] and not lock:
            a["lost"] += 1
        a["lock"] = lock

    def flush(self):
        for a in list(self._acc.values()):
            self._write(a)
        self._acc.clear()

    def _write(self, a):
        if not a["q"]:
            return
        t = a["minute"] * 60
        path = self.dir / time.strftime("%Y-%m-%d.csv", time.localtime(t))
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            new = not path.exists()
            with path.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(FIELDS)
                q, c = a["q"], a["cnr"]
                unc = 0
                if a["unc"] is not None and a["unc0"] is not None:
                    unc = max(0, a["unc"] - a["unc0"])
                w.writerow([int(t), a["name"], a["mux"], f"{min(q):.3f}",
                            f"{sum(q) / len(q):.3f}", f"{max(q):.3f}",
                            f"{min(c):.2f}" if c else "", f"{sum(c) / len(c):.2f}" if c else "",
                            f"{max(c):.2f}" if c else "", f"{a['ber']:.3g}", unc, a["lost"]])
        except OSError:
            pass
        self._prune()

    def _prune(self):
        cutoff = self.clock() - KEEP_DAYS * 86400
        for p in self.dir.glob("*.csv"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass

    def read(self, days=7, channel_name=None):
        """Rows of the last *days* days (dicts with numbers), oldest first."""
        since = self.clock() - days * 86400
        rows = []
        for p in sorted(self.dir.glob("*.csv")):
            try:
                with p.open(encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        try:
                            t = int(r["time"])
                        except (KeyError, ValueError):
                            continue
                        if t < since or (channel_name and r.get("channel") != channel_name):
                            continue
                        row = {"time": t, "channel": r.get("channel", ""), "mux": r.get("mux", "")}
                        for k in FIELDS[3:]:
                            try:
                                row[k] = float(r[k]) if r.get(k) not in (None, "") else None
                            except ValueError:
                                row[k] = None
                        rows.append(row)
            except OSError:
                continue
        return rows
