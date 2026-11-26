# SPDX-License-Identifier: GPL-2.0-or-later
"""Debug mode: a log of what LinDVB does, to attach to a bug report.

Switched on in Preferences ▸ Debug (setting ``debug_mode``) or with the
environment variable LINDVB_DEBUG=1 (LINDVB_HBBTV_DEBUG=1 too). It writes
~/.cache/lindvb/debug.log: a header with the versions of LinDVB, Python,
GTK and GStreamer, then one line per event (channel changes, tuning,
playback, recordings, status messages, HbbTV), the GStreamer warnings and
errors, everything printed to the terminal and every unhandled exception.

The file is kept small (two files of at most MAX_BYTES each) and holds no
personal data: the home folder becomes ``~``, the user name ``<user>``,
passwords and tokens in addresses ``***``; PINs, passwords and keys of the
settings are left out of the header.

Logging never raises: a full disk or a broken terminal only lose lines."""

import os
import platform
import re
import sys
import threading
import time
from pathlib import Path

from .config import APP_NAME, CACHE_DIR

MAX_BYTES = 2 * 1024 * 1024     # per file; debug.log plus one older debug.log.1
ENV = bool(os.environ.get("LINDVB_DEBUG") or os.environ.get("LINDVB_HBBTV_DEBUG"))

_lock = threading.RLock()
_on = False
_file = None
_path = None
_hooks_installed = False
_gst_hooked = False
_orig = {}


def path():
    """The current log file."""
    return _path or (CACHE_DIR / "debug.log")


def files():
    """The log files there are, oldest first."""
    p = path()
    return [f for f in (p.with_name(p.name + ".1"), p) if f.exists()]


def enabled():
    return _on


# -- privacy -----------------------------------------------------------------

_SECRET_KEY = re.compile(r"pin|pass|secret|token|key|cookie|uuid|auth|salt|session", re.I)
_URL_USER = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s@:]+(?::[^/\s@]*)?@", re.I)
_URL_SECRET = re.compile(
    r"([?&;](?:[a-z0-9_-]*(?:token|key|pass|pwd|auth|sig|signature|session|sid|secret|"
    r"hash|user|username|login|email|mac|serial)[a-z0-9_-]*)=)[^&#\s\"']+", re.I)


def _private_words():
    out = []
    try:
        home = str(Path.home())
        if home and home != "/":
            out.append((home, "~"))
    except (RuntimeError, KeyError):
        pass
    try:
        import getpass
        user = getpass.getuser()
        if user and len(user) > 2 and user != "root":
            out.append((user, "<user>"))
    except Exception:  # noqa: BLE001 - no user name: nothing to hide
        pass
    return out


_PRIVATE = _private_words()


def scrub(text):
    """*text* without the home folder, the user name and credentials in addresses."""
    text = str(text)
    for word, repl in _PRIVATE:
        text = text.replace(word, repl)
    text = _URL_USER.sub(r"\1***@", text)
    return _URL_SECRET.sub(r"\1***", text)


def settings_summary(settings):
    """The settings as ``key=value`` pairs, without secrets and lists of things."""
    parts = []
    for k in sorted(settings):
        v = settings[k]
        if _SECRET_KEY.search(k):
            continue
        if isinstance(v, (list, dict)):
            v = f"<{len(v)} entries>"
        parts.append(f"{k}={v!r}")
    return scrub(", ".join(parts))


# -- writing -----------------------------------------------------------------

def write(category, *parts):
    """Add a line to the log (nothing when debug mode is off)."""
    if not _on:
        return
    try:
        msg = " ".join(str(p) for p in parts)
    except Exception:  # noqa: BLE001 - logging must never break LinDVB
        return
    t = time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"
    thread = threading.current_thread()
    where = "" if thread is threading.main_thread() else f" [{thread.name}]"
    _emit(f"{stamp} {category}{where}: {scrub(msg)}")


def log(*parts):
    write("LinDVB", *parts)


def _fd_kind(target):
    """The kind of an open file, from its /proc/self/fd link."""
    if target.startswith("anon_inode:"):
        return target[len("anon_inode:"):].strip("[]") or "anon_inode"
    for prefix, kind in (("socket:", "socket"), ("pipe:", "pipe"), ("/dev/dri/", "dri"),
                         ("/dev/snd/", "snd"), ("/dev/", "dev"), ("/memfd:", "memfd"),
                         ("/dmabuf:", "dmabuf")):
        if target.startswith(prefix):
            return kind
    return "file"


def _dmabuf_exporter(fdinfo):
    """(exporting driver, bytes) of a graphics buffer, from its /proc/self/fdinfo
    entry ("exp_name: i915", "size: 3133440"); ("?", 0) when unreadable."""
    fields = {}
    try:
        with open(fdinfo, encoding="ascii", errors="replace") as f:
            for line in f:
                key, _sep, value = line.partition(":")
                fields[key.strip()] = value.strip()
    except OSError:
        pass
    try:
        size = int(fields.get("size", "0"))
    except ValueError:
        size = 0
    return fields.get("exp_name") or "?", size


def _human(size):
    return (f"{size / 1e9:.1f} GB" if size >= 1e9 else f"{size / 1e6:.0f} MB" if size >= 1e6
            else f"{size / 1e3:.0f} KB")


def _dmabuf_part(dmabufs, sizes=None):
    """ "graphics buffers i915 355 (1.1 GB); sizes 300x 24 KB, 17x 3 MB" from
    {driver: [count, bytes]} and {bytes: count} (the three most frequent sizes
    tell picture buffers from small ones)."""
    if not dmabufs:
        return ""
    parts = ", ".join(f"{name} {n} ({size / 1e9:.1f} GB)" if size >= 1e9 else
                      f"{name} {n} ({size / 1e6:.0f} MB)"
                      for name, (n, size) in sorted(dmabufs.items(), key=lambda kv: -kv[1][0]))
    out = f"; graphics buffers {parts}"
    if sizes:
        top = sorted(sizes.items(), key=lambda kv: (-kv[1], -kv[0]))[:3]
        out += "; sizes " + ", ".join(f"{n}x {_human(size)}" for size, n in top)
    return out


# -- objects that should go with their pipeline ------------------------------
# Debug mode counts the pipelines and GPU elements LinDVB made that still exist
# (GObject weak references): graphics buffers that pile up while the count of
# these stays flat are held below LinDVB (driver, GStreamer); a count that
# grows with every channel change means LinDVB keeps old pipelines alive.
_alive = {}


_tracked = set()        # (kind, name) of the GStreamer objects counted now


def track(obj, kind):
    """Count *obj* under *kind* until it is finalized (debug mode only). A
    GStreamer object (names are unique per factory) is counted once, however
    often it is reported: deep-element-added names the elements of a bin
    again when playsink takes the bin in."""
    if not _on or obj is None:
        return
    name = obj.get_name() if hasattr(obj, "get_name") else None
    key = (kind, name)
    with _lock:
        if name is not None:
            if key in _tracked:
                return
            _tracked.add(key)
        _alive[kind] = _alive.get(kind, 0) + 1

    def gone(*_a):
        with _lock:
            _alive[kind] -= 1
            _tracked.discard(key)
    obj.weak_ref(gone)


def alive_summary():
    """ "alive: pipeline 1, vah264dec 1" (what track() counts now), or ""."""
    with _lock:
        parts = [f"{k} {n}" for k, n in sorted(_alive.items()) if n]
    return "alive: " + ", ".join(parts) if parts else ""


def open_files_summary(fd_dir="/proc/self/fd"):
    """The files LinDVB holds open, counted by kind ("open files 41/1024: socket 12,
    pipe 8, ..."); the most frequent path is named when one is open several
    times, graphics buffers (dmabuf) by the driver that made them. Empty when
    debug mode is off or the system has no /proc."""
    if not _on:
        return ""
    kinds, paths, dmabufs, sizes = {}, {}, {}, {}
    info_dir = os.path.join(os.path.dirname(fd_dir), "fdinfo")
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return ""
    for name in names:
        try:
            target = os.readlink(os.path.join(fd_dir, name))
        except OSError:
            continue
        kind = _fd_kind(target)
        kinds[kind] = kinds.get(kind, 0) + 1
        if kind in ("file", "dev", "memfd"):
            paths[target] = paths.get(target, 0) + 1
        elif kind == "dmabuf":
            driver, size = _dmabuf_exporter(os.path.join(info_dir, name))
            entry = dmabufs.setdefault(driver, [0, 0])
            entry[0] += 1
            entry[1] += size
            sizes[size] = sizes.get(size, 0) + 1
    try:
        import resource
        limit = f"/{resource.getrlimit(resource.RLIMIT_NOFILE)[0]}"
    except (ImportError, OSError, ValueError):
        limit = ""
    parts = ", ".join(f"{k} {n}" for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))
    out = f"open files {sum(kinds.values())}{limit}: {parts}"
    top = max(paths.items(), key=lambda kv: kv[1], default=None)
    if top and top[1] > 2:
        out += f"; {top[1]}x {top[0]}"
    return out + _dmabuf_part(dmabufs, sizes)


def describe_channel(c):
    """A channel in one line: name, delivery system and tuning data (no stream login)."""
    if c is None:
        return "-"
    try:
        if c.is_iptv or getattr(c, "url", ""):
            return f"{c.name!r} ({c.delsys}) {c.url}"
        return (f"{c.name!r} ({c.delsys} {c.frequency} kHz {c.polarity}{' ' if c.polarity else ''}"
                f"SR {c.symbol_rate} sid {c.sid} tsid {c.tsid} onid {c.onid}"
                f"{' encrypted' if c.encrypted else ''}{' radio' if c.radio else ''})")
    except Exception:  # noqa: BLE001
        return repr(getattr(c, "name", c))


def _emit(line):
    global _file
    with _lock:
        if not _on:
            return
        try:
            if _file is None:
                p = path()
                p.parent.mkdir(parents=True, exist_ok=True)
                _file = open(p, "a", encoding="utf-8", errors="replace")
            _file.write(line.rstrip("\n") + "\n")
            _file.flush()
            if _file.tell() > MAX_BYTES:
                _rotate()
        except (OSError, ValueError):
            pass


def _rotate():
    global _file
    try:
        _file.close()
    except OSError:
        pass
    _file = None
    p = path()
    try:
        os.replace(p, p.with_name(p.name + ".1"))
    except OSError:
        pass


def clear():
    """Delete the log files (a new one starts with the next line)."""
    global _file
    with _lock:
        if _file is not None:
            try:
                _file.close()
            except OSError:
                pass
            _file = None
        for f in files():
            try:
                f.unlink()
            except OSError:
                pass
    if _on:
        _header("log cleared")


def save_as(target):
    """Copy the log (older file first) to *target*."""
    with _lock:
        if _file is not None:
            try:
                _file.flush()
            except (OSError, ValueError):
                pass
        with open(target, "w", encoding="utf-8") as out:
            for f in files():
                out.write(f.read_text(encoding="utf-8", errors="replace"))


# -- switching on and off ------------------------------------------------------

def configure(settings=None, log_path=None):
    """Follow the settings (``debug_mode``) or LINDVB_DEBUG; call again after a change."""
    global _on, _path, _file
    want = ENV or bool(settings is not None and settings.get("debug_mode", False))
    with _lock:
        if log_path is not None and Path(log_path) != _path:
            if _file is not None:
                _file.close()
                _file = None
            _path = Path(log_path)
        if want == _on:
            return
        if not want:
            write("LinDVB", "debug mode switched off")
            _on = False
            if _file is not None:
                try:
                    _file.close()
                except OSError:
                    pass
                _file = None
            _uninstall()
            return
        _on = True
    _install()
    _header("debug mode switched on", settings)


def _header(what, settings=None):
    from . import __version__
    lines = [f"===== {APP_NAME} {__version__}: {what} ====="]
    lines.append(f"Python {platform.python_version()} on {platform.platform(terse=True)}"
                 f" ({platform.machine()})")
    pkg = ("AppImage" if os.environ.get("APPIMAGE") else
           "Flatpak" if os.environ.get("FLATPAK_ID") else
           "Snap" if os.environ.get("SNAP") else "source / system package")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "?")
    session = os.environ.get("XDG_SESSION_TYPE", "?")
    lines.append(f"Package: {pkg}; desktop: {desktop} ({session}); "
                 f"language: {os.environ.get('LANG', '?')}")
    try:
        import gi
        lines.append(f"PyGObject {gi.__version__}")
        Gtk = sys.modules.get("gi.repository.Gtk")     # only when LinDVB loaded it
        if Gtk is not None:
            lines.append(f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}."
                         f"{Gtk.get_micro_version()}")
    except Exception:  # noqa: BLE001
        pass
    try:
        Gst = sys.modules.get("gi.repository.Gst")
        if Gst is not None and Gst.is_initialized():
            lines.append(Gst.version_string())
    except Exception:  # noqa: BLE001
        pass
    try:
        adapters = sorted(p.name for p in Path("/dev/dvb").iterdir())
        lines.append("DVB adapters: " + (", ".join(adapters) or "none"))
    except OSError:
        lines.append("DVB adapters: none")
    if settings is not None:
        lines.append("Settings: " + settings_summary(settings))
    for line in lines:
        write("LinDVB", line)


# -- what is captured without log calls ----------------------------------------

class _Tee:
    """sys.stdout / sys.stderr that also go into the log."""

    def __init__(self, stream, name):
        self._stream = stream
        self._name = name
        self._buf = ""

    def write(self, s):
        try:
            n = self._stream.write(s)
        except UnicodeEncodeError:
            enc = getattr(self._stream, "encoding", None) or "ascii"
            n = self._stream.write(s.encode(enc, "replace").decode(enc))
        if _on and isinstance(s, str):
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    write(self._name, line)
        return n

    def __getattr__(self, name):
        return getattr(self._stream, name)


def terminal():
    """sys.stdout without the copy into the log (for lines logged already)."""
    out = sys.stdout
    return out._stream if isinstance(out, _Tee) else out


def _excepthook(kind, value, tb):
    import traceback
    write("Exception", "".join(traceback.format_exception(kind, value, tb)).rstrip())
    _orig["excepthook"](kind, value, tb)


def _thread_excepthook(args):
    import traceback
    write("Exception", f"in thread {getattr(args.thread, 'name', '?')}: " +
          "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                             args.exc_traceback)).rstrip())
    _orig["threading"](args)


def _install():
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    _orig["excepthook"] = sys.excepthook
    _orig["threading"] = threading.excepthook
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    if sys.stdout is not None and not isinstance(sys.stdout, _Tee):
        _orig["stdout"] = sys.stdout
        sys.stdout = _Tee(sys.stdout, "Terminal")
    if sys.stderr is not None and not isinstance(sys.stderr, _Tee):
        _orig["stderr"] = sys.stderr
        sys.stderr = _Tee(sys.stderr, "Terminal")
    _hook_gstreamer()


def _uninstall():
    global _hooks_installed
    if not _hooks_installed:
        return
    _hooks_installed = False
    if sys.excepthook is _excepthook:
        sys.excepthook = _orig["excepthook"]
    if threading.excepthook is _thread_excepthook:
        threading.excepthook = _orig["threading"]
    for name in ("stdout", "stderr"):
        if name in _orig and isinstance(getattr(sys, name), _Tee):
            setattr(sys, name, _orig.pop(name))
    _unhook_gstreamer()


_GST_PREV = None


def _gst_log(category, level, file, function, line, obj, message, *_user):
    if not _on:
        return
    try:
        text = message.get() if hasattr(message, "get") else str(message)
        # no obj.get_name(): GStreamer may log with the object's lock held
        write("GStreamer", f"{level.value_nick.upper()} {category.get_name()} "
                           f"{function}:{line} {text}")
    except Exception:  # noqa: BLE001
        pass


def _hook_gstreamer():
    """GStreamer's warnings and errors into the log (they go to no terminal otherwise)."""
    global _gst_hooked, _GST_PREV
    if _gst_hooked:
        return
    try:
        Gst = sys.modules.get("gi.repository.Gst")
        if Gst is None or not Gst.is_initialized():
            return
        _GST_PREV = Gst.debug_get_default_threshold()
        Gst.debug_add_log_function(_gst_log, None)
        if _GST_PREV < Gst.DebugLevel.WARNING:
            Gst.debug_set_default_threshold(Gst.DebugLevel.WARNING)
        Gst.debug_set_active(True)
        _gst_hooked = True
    except Exception:  # noqa: BLE001 - a GStreamer without a debug system
        pass


def _unhook_gstreamer():
    global _gst_hooked
    if not _gst_hooked:
        return
    try:
        from gi.repository import Gst
        Gst.debug_remove_log_function(_gst_log)
        if _GST_PREV is not None:
            Gst.debug_set_default_threshold(_GST_PREV)
    except Exception:  # noqa: BLE001
        pass
    _gst_hooked = False
