# SPDX-License-Identifier: GPL-2.0-or-later
"""Where and how large the capture buffer (timeshift) is.

Every capture writes its transport stream into rolling segment files
(lindvb.tuner.SegmentStore); live view, timeshift, recordings and teletext
all read from them. The "timeshift_mode" preference decides where they live:

* "disk": in the timeshift folder (the default), at most the set size, and
  never more than the free space of that disk minus a reserve;
* "ram": in a tmpfs (/dev/shm), at most half of the memory, and never more
  than what is available right now minus a reserve;
* "off": a small buffer in memory that only carries the live picture and
  recordings; pause and jumping back are not offered.

A capture that only records (nobody watches it) keeps the small buffer in
RAM mode too, so a background recording does not take a second large share
of the memory; it grows when the live view joins it (Capture.set_live).
"""

import os
import re
import shutil
import stat
import time
from pathlib import Path

MB = 1024 * 1024
MODES = ("off", "ram", "disk")
STORES = ("ram", "disk")          # where the buffer lives while timeshift is on

SEGMENT_BYTES = 64 * MB           # disk and RAM buffers
SMALL_SEGMENT_BYTES = 16 * MB     # the small buffer (off, RAM recordings)
SMALL_SEGMENTS = 4                # 64 MB: a few seconds of a full transponder
MIN_MB = 128

DISK_DEFAULT_MB = 4096
RAM_DEFAULT_MB = 1024
DISK_MAX_MB = 200_000
RAM_SHARE = 0.5                   # of the total memory
DISK_RESERVE_MB = 2048            # left free on the disk (recordings)
RAM_RESERVE_MB = 1024             # left of the available memory

STALE_SECONDS = 600               # a buffer nothing wrote to for so long is left over
_BUFFER_DIR = re.compile(r"a\d+-\w+$")     # tempfile.mkdtemp(prefix="a<adapter>-")
_SEGMENT = re.compile(r"seg\d+\.ts$")


def mode(settings):
    m = settings.get("timeshift_mode", "disk")
    return m if m in MODES else "disk"


def store(settings):
    """Where the buffer lives when timeshift is on: "ram" or "disk"."""
    m = mode(settings)
    if m in STORES:
        return m
    s = settings.get("timeshift_store", "disk")
    return s if s in STORES else "disk"


def enabled(settings):
    return mode(settings) != "off"


def max_mb(settings):
    """The buffer size the user set for the current mode."""
    key = "timeshift_ram_mb" if mode(settings) == "ram" else "timeshift_max_mb"
    default = RAM_DEFAULT_MB if mode(settings) == "ram" else DISK_DEFAULT_MB
    try:
        return max(MIN_MB, int(settings.get(key, default)))
    except (TypeError, ValueError):
        return default


def _meminfo():
    """(MemTotal, MemAvailable) in MB, or (None, None)."""
    vals = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                if k in ("MemTotal", "MemAvailable"):
                    vals[k] = int(rest.split()[0]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return vals.get("MemTotal"), vals.get("MemAvailable")


def ram_limit_mb():
    """The largest RAM buffer the preferences offer (half of the memory)."""
    total, _ = _meminfo()
    return max(MIN_MB, int(total * RAM_SHARE)) if total else 8192


def private_dir(path):
    """*path* as a folder only this user can enter, or None when it cannot be
    one. /dev/shm is writable by everyone: another user could have put a
    folder or a symbolic link there first, under the name LinDVB uses."""
    path = Path(path)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError:
        return None
    try:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            return None
        if st.st_mode & 0o077:
            os.chmod(path, 0o700)
    except OSError:
        return None
    return path


def ram_dir():
    """A tmpfs folder of this user for buffers in memory (created 0700), or
    None. /dev/shm is a tmpfs of up to half of the memory; the per-user
    XDG_RUNTIME_DIR is private but usually only a tenth of it."""
    uid = os.getuid()
    shm = Path("/dev") / "shm"
    snap = os.environ.get("SNAP_INSTANCE_NAME") or os.environ.get("SNAP_NAME")
    if shm.is_dir() and os.access(shm, os.W_OK):
        # strict snaps may only use names that start with snap.<name>.
        d = private_dir(shm / (f"snap.{snap}.lindvb-{uid}" if snap else f"lindvb-{uid}"))
        if d is not None:
            return d
    run = os.environ.get("XDG_RUNTIME_DIR")
    if run and os.path.isdir(run):
        return private_dir(Path(run) / "lindvb-timeshift")
    return None


def disk_dir(settings):
    from .config import DEFAULTS
    return Path(settings.get("timeshift_dir") or DEFAULTS["timeshift_dir"])


def _free_mb(path):
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free // MB
    except OSError:
        return None


class Plan:
    """Where a capture writes and how large its buffer is."""

    def __init__(self, directory, segment_bytes, max_files, note="", full=True):
        self.dir = Path(directory)
        self.segment_bytes = segment_bytes
        self.max_files = max_files
        self.note = note                  # why the buffer is smaller than set ("" if not)
        self.full = full                  # False: the small buffer

    @property
    def size_mb(self):
        return self.segment_bytes * self.max_files // MB

    def __repr__(self):
        return f"Plan({self.dir}, {self.size_mb} MB, full={self.full})"


def _small(directory):
    return Plan(directory, SMALL_SEGMENT_BYTES, SMALL_SEGMENTS, full=False)


def plan(settings, live=True, _mem=None, _free=None):
    """The buffer of a new capture. *live*: the live view watches it."""
    from .i18n import _
    m = mode(settings)
    ram = ram_dir()
    if m == "off" or (m == "ram" and not live):
        return _small(ram or disk_dir(settings))
    want = max_mb(settings)
    if m == "ram":
        if ram is None:
            return _plan_disk(settings, want, _free)
        total, avail = _mem if _mem is not None else _meminfo()
        size, note = want, ""
        if total:
            size = min(size, int(total * RAM_SHARE))
        if avail is not None and avail - RAM_RESERVE_MB < size:
            size = avail - RAM_RESERVE_MB
            note = _("not enough free memory")
        return _sized(ram, size, note)
    return _plan_disk(settings, want, _free)


def _plan_disk(settings, want, _free=None):
    from .i18n import _
    base = disk_dir(settings)
    free = _free if _free is not None else _free_mb(base)
    size, note = want, ""
    if free is not None and free - DISK_RESERVE_MB < size:
        size = free - DISK_RESERVE_MB
        note = _("the disk is almost full")
    return _sized(base, size, note, small_dir=ram_dir() or base)


def _sized(directory, size, reason, small_dir=None):
    """A full buffer of *size* MB, or the small one when that is too little;
    *reason* why it is smaller than set ("" if it is not)."""
    from .i18n import _
    if size < MIN_MB:
        p = _small(small_dir or directory)
        p.note = _("Timeshift is off for this channel: {reason}").format(reason=reason)
        return p
    p = Plan(directory, SEGMENT_BYTES, max(2, size * MB // SEGMENT_BYTES))
    if reason:
        p.note = _("Timeshift buffer reduced to {mb} MB: {reason}").format(mb=p.size_mb,
                                                                          reason=reason)
    return p


def bases(settings):
    out = [disk_dir(settings)]
    r = ram_dir()
    if r is not None:
        out.append(r)
    return out


def cleanup_stale(settings, now=None):
    """Remove buffers a crashed or killed LinDVB left behind (in memory they
    would hold RAM until the next reboot). A buffer is left over when nothing
    was written to it for STALE_SECONDS; running captures write all the time."""
    now = now or time.time()
    removed = []
    for base in bases(settings):
        try:
            dirs = [d for d in base.iterdir() if d.is_dir() and _BUFFER_DIR.match(d.name)]
        except OSError:
            continue
        for d in dirs:
            try:
                files = list(d.iterdir())
                # only folders of segments (the timeshift folder may be any folder)
                if any(not (f.is_file() and _SEGMENT.match(f.name)) for f in files):
                    continue
                newest = max([f.stat().st_mtime for f in files] or [d.stat().st_mtime])
            except OSError:
                continue
            if now - newest > STALE_SECONDS:
                shutil.rmtree(d, ignore_errors=True)
                removed.append(d)
    return removed


def duration_hint(mb):
    """'≈ 55 min HD · 22 min UHD' for a buffer of *mb* megabytes."""
    from .i18n import _

    def mins(mbit):
        return mb * MB * 8 / (mbit * 1_000_000) / 60

    def fmt(m):
        return _("{m} min").format(m=int(m))
    return _("≈ {sd} SD · {hd} HD · {uhd} UHD").format(sd=fmt(mins(4)), hd=fmt(mins(10)),
                                                       uhd=fmt(mins(25)))
