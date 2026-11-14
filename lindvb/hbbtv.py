# SPDX-License-Identifier: GPL-2.0-or-later
"""HbbTV 2.0.4 terminal logic (ETSI TS 102 796 v1.7.1) without the UI.

What the browser needs to know about the terminal: the user agent, the key
codes and key sets, the capabilities document, the OIPF channel objects for
the channel list, and which application to run when the AIT changes.
The browser itself (WebKitGTK) and the JavaScript side live in
lindvb.ui.hbbtv and lindvb/hbbtv.js.
"""

import os
import shutil
import subprocess
from pathlib import Path

from . import ait as aitmod

SPEC_VERSION = "1.7.1"            # HbbTV 2.0.4
APP_WIDTH, APP_HEIGHT = 1280, 720  # the coordinate system of every HbbTV app

JS_FILE = Path(__file__).with_name("hbbtv.js")
DASH_JS = Path(__file__).with_name("vendor") / "dash.mediaplayer.min.js"


_SANDBOX = None


def sandbox_ok(run=subprocess.run, which=shutil.which, env=os.environ):
    """Can WebKit start its sandboxed web process here?

    WebKitGTK runs every page inside bubblewrap, which needs unprivileged user
    namespaces. Ubuntu 24.04 forbids those for programs without an AppArmor
    profile (kernel.apparmor_restrict_unprivileged_userns), and WebKit then
    aborts the whole program instead of failing the page. Flatpak and Snap
    bring their own sandbox; WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS means
    the user has decided. Probed once per run.
    """
    global _SANDBOX
    if _SANDBOX is not None:
        return _SANDBOX
    if (env.get("WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS") or env.get("FLATPAK_ID")
            or env.get("SNAP") or Path("/.flatpak-info").exists()):
        _SANDBOX = True
        return True
    bwrap = which("bwrap")
    if not bwrap:
        _SANDBOX = True      # WebKit built without bubblewrap, or it brings its own
        return True
    try:
        r = run([bwrap, "--ro-bind", "/", "/", "--unshare-user", "--unshare-net", "true"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        _SANDBOX = r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        _SANDBOX = False
    return _SANDBOX


def page_script(init_json):
    """Everything injected before an application page: the terminal's state,
    dash.js (as window.__lindvbDash, no global `dashjs` that could clash with
    an app's own copy) and the OIPF objects."""
    parts = ["window.__lindvbInit = " + init_json + ";"]
    if DASH_JS.exists():
        # dash.js sets window.dashjs and refers to `dashjs` later: give it a
        # variable of its own in the wrapper and put back what window had
        parts.append("try { window.__lindvbDash = (function () { var module = { exports: {} }, "
                     "exports = module.exports, dashjs, had = 'dashjs' in window, "
                     "prev = window.dashjs;\n" + DASH_JS.read_text("utf-8") +
                     "\n; dashjs = window.dashjs || module.exports; if (had) { window.dashjs = prev; } "
                     "else { try { delete window.dashjs; } catch (e) { window.dashjs = undefined; } } "
                     "return dashjs; })(); } catch (e) { console.error('dash.js', e); }")
    parts.append(JS_FILE.read_text("utf-8"))
    return "\n".join(parts)


# MIME types of HbbTV documents (WebKit does not render them itself)
DOCUMENT_TYPES = ("application/vnd.hbbtv.xhtml+xml", "application/ce-html+xml")


def terminal_version():
    """LinDVB's version, for the user agent and localSystem.softwareVersion."""
    try:
        from importlib.metadata import version
        return version("lindvb")
    except Exception:
        return ""


def user_agent(version="", vendor="LinDVB", model="LinDVB", family="io.github.thommy87.LinDVB"):
    """User agent with the HbbTV token (TS 102 796 7.3.2.4)."""
    return ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            f"HbbTV/{SPEC_VERSION} (+TRICKMODE; {vendor}; {model}; {version}; ; {family}; ) "
            "Safari/605.1.15")


# -- keys ------------------------------------------------------------------------

# KeyEvent.VK_* codes (OIPF DAE / CEA-2014)
VK = {
    "RED": 403, "GREEN": 404, "YELLOW": 405, "BLUE": 406,
    "LEFT": 37, "UP": 38, "RIGHT": 39, "DOWN": 40, "ENTER": 13, "BACK": 461,
    "PLAY": 415, "PAUSE": 19, "PLAY_PAUSE": 402, "STOP": 413, "FAST_FWD": 417, "REWIND": 412,
    "TRACK_NEXT": 425, "TRACK_PREV": 424, "INFO": 457, "PAGE_UP": 33, "PAGE_DOWN": 34,
    "TELETEXT": 459, "SUBTITLE": 460, "CHANNEL_UP": 427, "CHANNEL_DOWN": 428,
    **{str(n): 48 + n for n in range(10)},
}

# Keyset masks (application.privateData.keyset)
RED, GREEN, YELLOW, BLUE = 0x1, 0x2, 0x4, 0x8
NAVIGATION, VCR, SCROLL, INFO, NUMERIC, ALPHA, OTHER = 0x10, 0x20, 0x40, 0x80, 0x100, 0x200, 0x400

KEY_GROUP = {
    VK["RED"]: RED, VK["GREEN"]: GREEN, VK["YELLOW"]: YELLOW, VK["BLUE"]: BLUE,
    **{VK[k]: NAVIGATION for k in ("LEFT", "UP", "RIGHT", "DOWN", "ENTER", "BACK")},
    **{VK[k]: VCR for k in ("PLAY", "PAUSE", "PLAY_PAUSE", "STOP", "FAST_FWD", "REWIND",
                            "TRACK_NEXT", "TRACK_PREV")},
    VK["PAGE_UP"]: SCROLL, VK["PAGE_DOWN"]: SCROLL, VK["INFO"]: INFO,
    **{48 + n: NUMERIC for n in range(10)},
}

# GDK key names -> VK codes. Colour keys: F5..F8 on a keyboard, the XF86
# keysyms a remote control (LIRC / evdev) produces.
GDK_KEYS = {
    "F5": "RED", "F6": "GREEN", "F7": "YELLOW", "F8": "BLUE",
    "XF86Red": "RED", "XF86Green": "GREEN", "XF86Yellow": "YELLOW", "XF86Blue": "BLUE",
    "Left": "LEFT", "Up": "UP", "Right": "RIGHT", "Down": "DOWN",
    "Return": "ENTER", "KP_Enter": "ENTER", "BackSpace": "BACK", "XF86Back": "BACK",
    "XF86AudioPlay": "PLAY_PAUSE", "XF86AudioPause": "PAUSE", "XF86AudioStop": "STOP",
    "XF86AudioForward": "FAST_FWD", "XF86AudioRewind": "REWIND",
    "XF86AudioNext": "TRACK_NEXT", "XF86AudioPrev": "TRACK_PREV",
    "Page_Up": "PAGE_UP", "Page_Down": "PAGE_DOWN",
    **{str(n): str(n) for n in range(10)}, **{f"KP_{n}": str(n) for n in range(10)},
}


def key_code(gdk_name):
    """VK code for a GDK key name, or 0."""
    name = GDK_KEYS.get(gdk_name)
    return VK.get(name, 0) if name else 0


def wanted(code, keyset, other_keys=()):
    """True when the running application asked for this key (TS 102 796 10.2.2)."""
    group = KEY_GROUP.get(code)
    if group is None:
        return bool(keyset & OTHER) and code in other_keys
    return bool(keyset & group)


# ISO 639-1 -> ISO 639-2/T for the interface languages
LANG3 = {"de": "deu", "en": "eng", "fr": "fra", "it": "ita", "es": "spa", "nl": "nld",
         "pl": "pol", "pt": "por", "da": "dan", "sv": "swe", "fi": "fin", "et": "est",
         "lt": "lit", "lv": "lav", "cs": "ces", "sk": "slk", "sl": "slv", "hr": "hrv",
         "hu": "hun", "ro": "ron", "bg": "bul", "el": "ell", "ga": "gle", "mt": "mlt",
         "ar": "ara", "ru": "rus", "uk": "ukr", "ja": "jpn",
         "ka": "kat", "zh": "zho"}


def viewer_language(settings, ui_language=""):
    """ISO 639-2 language the applications are told (preferred*Language)."""
    lang = settings.get("hbbtv_language") or settings.get("rec_audio_language") or ""
    if lang:
        return lang
    code = (settings.get("language") or ui_language or "").split("_")[0].lower()
    return LANG3.get(code, "deu")


# -- capabilities ----------------------------------------------------------------

def capabilities_xml(delivery=("DVB_S", "DVB_C", "DVB_T")):
    """application/oipfCapabilities.xmlCapabilities (TS 102 796 10.2.4)."""
    ui = "OITF_HD_UIPROF+" + "+".join(delivery) + "+TRICKMODE"
    video = [("MP4_AVC_HD_25_HEAAC", "video/mp4"), ("MP4_AVC_SD_25_HEAAC", "video/mp4"),
             ("TS_AVC_HD_25_HEAAC", "video/mpeg"), ("MP4_HEVC_UHD_25_HEAAC_EBUTTD", "video/mp4"),
             ("MP4_AVC_HD_25_HEAAC_EBUTTD", "application/dash+xml"),
             ("MP4_HEVC_HD_25_HEAAC_EBUTTD", "application/dash+xml")]
    audio = [("MPEG1_L3", "audio/mpeg"), ("HEAAC", "audio/mp4"), ("MP4_HEAAC", "audio/mp4"),
             ("MP4_E-AC3", "audio/mp4")]
    rows = [f'<ui_profile name="{ui}"><ext>'
            '<parentalcontrol schemes="dvb-si">true</parentalcontrol>'
            '<clientMetadata type="dvb-si">true</clientMetadata>'
            '<temporalClipping/></ext></ui_profile>']
    rows += [f'<audio_profile name="{n}" type="{t}"/>' for n, t in audio]
    rows += [f'<video_profile name="{n}" type="{t}"/>' for n, t in video]
    rows += ['<video_display_format name="HD_1080_25"/>', "<html5_media>true</html5_media>"]
    return "<profilelist>" + "".join(rows) + "</profilelist>"


# -- channels --------------------------------------------------------------------

# OIPF Channel.idType
ID_IDS = {"dvb-c": 10, "dvb-c2": 14, "dvb-s": 11, "dvb-s2": 15, "turbo": 15,
          "dvb-t": 12, "dvb-t2": 16, "iptv": 41}


def channel_dict(c, ccid):
    """The OIPF Channel object of a channel-list entry, as JSON for hbbtv.js."""
    return {"ccid": ccid, "name": c.name, "onid": c.onid, "tsid": c.tsid, "sid": c.sid,
            "idType": ID_IDS.get(c.delsys, 12 if not c.is_iptv else 41),
            "channelType": 1 if c.radio else 0, "majorChannel": c.number or c.lcn or 0,
            "dsd": "", "ipBroadcastID": c.url if c.is_iptv else "",
            "locked": False, "hidden": not c.visible, "isHD": False}


def event_dict(ev):
    """An OIPF Programme from an EPG event."""
    return {"name": ev.title, "description": ev.subtitle or ev.description,
            "longDescription": ev.description, "startTime": int(ev.start),
            "duration": int(ev.duration), "programmeID": str(ev.event_id or ev.programme_id),
            "programmeIDType": 1 if ev.event_id else 0,
            "parentalRatings": [{"name": str(ev.min_age), "scheme": "dvb-si",
                                 "value": ev.min_age}] if ev.min_age else []}


def broadcast_components(audio, audio_cur, subtitles, sub_cur):
    """AVComponent objects of the live picture for video/broadcast.getComponents().

    audio: [AudioTrack]; subtitles: [(label, language)]; *_cur: active index (-1 none)."""
    out = [{"type": 0, "index": 0, "language": "", "active": True, "encoding": "", "label": ""}]
    for i, t in enumerate(audio):
        lang = (t.language or "")[:3].lower()
        out.append({"type": 1, "index": i, "language": lang, "active": i == audio_cur,
                    "encoding": t.codec or "", "label": t.title or "",
                    "audioDescription": "description" in (t.title or "").lower(),
                    "audioChannels": t.channels or 0})
    for i, (label, lang) in enumerate(subtitles):
        out.append({"type": 2, "index": i, "language": (lang or "")[:3].lower(),
                    "active": i == sub_cur, "encoding": "", "label": label,
                    "hearingImpaired": "hearing" in label.lower()})
    return out


# -- DVB-I linked applications ----------------------------------------------------------

def dvbi_ait(apps, fetch):
    """One AIT for the linked HbbTV applications of a DVB-I channel
    (lindvb.dvbi._linked_apps): an XML AIT is fetched with *fetch(url) ->
    bytes* and read, an HTML application becomes an AUTOSTART application of
    its own. Applications that present the service (role "media") come first.
    None when no application is left."""
    merged = aitmod.Ait(app_type=aitmod.HBBTV_APP_TYPE, version=0)
    seen = set()
    for entry in sorted(apps, key=lambda a: a.get("role") != "media"):
        url, ctype = entry.get("url") or "", (entry.get("type") or "").lower()
        if ctype == aitmod.XML_AIT_TYPE or (not ctype and url.lower().endswith((".aitx", ".ait.xml"))):
            try:
                ait = aitmod.parse_xml_ait(fetch(url))
            except Exception:  # noqa: BLE001 - a network or server failure: skip this one
                ait = None
        else:
            ait = aitmod.ait_for_url(url)
        if ait and entry.get("role") == "media" and \
                not any(a.control_code == aitmod.AUTOSTART for a in ait.apps):
            ait.apps[0].control_code = aitmod.AUTOSTART
        for app in (ait.apps if ait else ()):
            if app.key in seen:
                continue
            seen.add(app.key)
            if entry.get("role") == "media":
                app.priority += 0x100            # the service's own application wins
            merged.apps.append(app)
    return merged if merged.apps else None


# -- DRM ------------------------------------------------------------------------------

# LinDVB has no licensed DRM system; these names only tell the user which one
# a protected video wanted (DASH-IF system ids, EME key systems, DVB CA ids).
DRM_SYSTEMS = {
    "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed": "Widevine",
    "com.widevine.alpha": "Widevine",
    "9a04f079-9840-4286-ab92-e65be0885f95": "PlayReady",
    "com.microsoft.playready": "PlayReady",
    "com.microsoft.playready.recommendation": "PlayReady",
    "5e629af5-38da-4063-8977-97ffbd9902d4": "Marlin",
    "urn:dvb:casystemid:19188": "Marlin",
    "94ce86fb-07ff-4f43-adb8-93d2fa968ca2": "FairPlay",
    "com.apple.fps": "FairPlay",
}


def drm_name(system):
    """'Widevine' for 'urn:uuid:EDEF8BA9-...' or 'com.widevine.alpha'; '' if unknown."""
    key = (system or "").strip().lower()
    if key.startswith("urn:uuid:"):
        key = key[9:]
    if key.startswith("com.widevine."):
        key = "com.widevine.alpha"
    return DRM_SYSTEMS.get(key, "")


# -- parental control ---------------------------------------------------------------

AGE_LIMITS = (0, 6, 12, 16)      # 0: off; else the age of the youngest viewer
PIN_TRIES = 3
PIN_LOCK_SECONDS = 300


def rating_age(rating):
    """Minimum age of a DVB parental rating (EN 300 468 6.2.28): 0x01..0x0F
    = rating + 3 years; 0 (undefined) and broadcaster codes count as none."""
    return rating + 3 if 0x01 <= rating <= 0x0F else 0


def min_age(ratings, country="DEU"):
    """Minimum age of [(country, rating)], the viewer's country first."""
    mine = [r for c, r in ratings if c.upper() == country.upper()]
    pool = mine or [r for _c, r in ratings]
    return max((rating_age(r) for r in pool), default=0)


def needs_pin(ratings, limit, country="DEU"):
    return bool(limit) and min_age(ratings, country) > limit


def hash_pin(pin, salt):
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", str(pin).encode(), bytes.fromhex(salt), 100_000).hex()


def new_salt():
    return os.urandom(16).hex()


class PinGuard:
    """Checks the youth protection PIN; locks for a while after wrong tries.

    verify() answers as OIPF verifyParentalControlPIN: 0 correct, 1 wrong,
    2 locked."""

    def __init__(self, settings, clock=None):
        import time
        self.settings = settings
        self.clock = clock or time.monotonic
        self.wrong = 0
        self.locked_until = 0.0

    @property
    def locked(self):
        return self.clock() < self.locked_until

    @property
    def has_pin(self):
        return bool(self.settings.get("hbbtv_pin") and self.settings.get("hbbtv_pin_salt"))

    def verify(self, pin):
        if self.locked:
            return 2
        if self.has_pin and hash_pin(pin, self.settings["hbbtv_pin_salt"]) == self.settings["hbbtv_pin"]:
            self.wrong = 0
            return 0
        self.wrong += 1
        if self.wrong >= PIN_TRIES:
            self.wrong = 0
            self.locked_until = self.clock() + PIN_LOCK_SECONDS
            return 2
        return 1


# -- application lifecycle ------------------------------------------------------

def parse_ait_url(url):
    """((org_id, app_id), '?query#fragment') of an application URL of the
    current AIT, dvb://current.ait/<org_id>.<app_id> in hex (TS 102 851,
    TS 102 796 6.2.2.6); None for any other URL."""
    low = (url or "").lower()
    if not low.startswith("dvb://current.ait/"):
        return None
    rest = url[len("dvb://current.ait/"):]
    cut = min([i for i in (rest.find("?"), rest.find("#")) if i >= 0] or [len(rest)])
    ids, extra = rest[:cut].strip("/"), rest[cut:]
    org, _, app = ids.partition(".")
    try:
        return (int(org, 16), int(app, 16)), extra
    except ValueError:
        return None


def append_query(url, extra):
    """The application URL with the query and fragment of a dvb://current.ait URL."""
    if not extra:
        return url
    query, _, frag = extra.partition("#")
    base, _, old_frag = url.partition("#")
    if query.startswith("?") and len(query) > 1:
        base += ("&" if "?" in base else "?") + query[1:]
    frag = frag or old_frag
    return base + ("#" + frag if frag else "")


class Lifecycle:
    """Decides which application runs (TS 102 796 6.2.2, 6.3).

    ``launch(url, app)`` and ``kill()`` are callbacks into the browser. The
    terminal follows the AIT: a new AUTOSTART app starts when nothing runs,
    a running broadcast-related app keeps going while the AIT still lists it
    (and not with KILL/DESTROY), and service-bound apps end on a channel
    change that does not signal them.
    """

    def __init__(self, launch, kill, autostart=True):
        self.launch_cb, self.kill_cb = launch, kill
        self.autostart = autostart
        self.ait = None
        self.running = None              # AitApp, or None for a broadcast-independent app
        self.running_url = ""
        self.suppressed = set()          # app keys the user (or the app) closed on this service
        self.allowed = None              # callable(app) -> bool: parental control
        self.on_blocked = None           # callable(app, start): ask for the PIN, then start()

    def channel_changed(self):
        """Before the new channel's AIT arrives."""
        self.ait = None
        self.suppressed = set()
        if self.active and (self.running is None or self.running.service_bound):
            self.stop()                  # broadcast-independent apps end on a zap too

    def no_signalling(self):
        """The new channel has no AIT at all (IPTV, radio without HbbTV)."""
        self.ait = None
        if self.active:
            self.stop()

    def on_ait(self, ait):
        self.ait = ait
        run = self.running
        if run is not None:
            now = ait.find(run.key)
            if now is None or now.control_code in (aitmod.KILL, aitmod.DESTROY):
                self.stop()
            else:
                self.running = now
                return
        if self.running_url and self.running is None:
            return                       # a broadcast-independent app keeps running
        if self.autostart:
            app = ait.autostart()
            if app is not None and app.key not in self.suppressed:
                self._gate(app, by_user=False)

    def _gate(self, app, by_user):
        """Start *app* unless parental control holds it back. Apps rated
        above the limit never start by themselves; the user is asked for the
        PIN when they start one."""
        if self.allowed is None or self.allowed(app):
            self.start(app)
            return True
        if by_user and self.on_blocked is not None:
            self.on_blocked(app, lambda: self.start(app))
            return True
        return False

    def start(self, app, url=None):
        self.running = app
        self.running_url = url or (self.ait.url(app) if self.ait else "")
        self.launch_cb(self.running_url, app)

    def start_service_app(self):
        """A DVB-I service that is an application: start it even with
        autostart off, since there is no picture without it."""
        if self.ait is None or self.active:
            return False
        app = self.ait.autostart()
        if app is None or app.key in self.suppressed:
            return False
        return self._gate(app, by_user=False)

    def red_button(self):
        """The user pressed red while no app takes keys: start the service's app."""
        if self.ait is None:
            return False
        app = self.ait.autostart()
        if app is None:
            return False
        self.suppressed.discard(app.key)
        return self._gate(app, by_user=True)

    def teletext_button(self):
        """The TEXT key: the digital teletext application, if the AIT has one."""
        app = self.ait.teletext() if self.ait else None
        if app is None:
            return False
        return self._gate(app, by_user=True)

    def create_application(self, url):
        """Application.createApplication(): an app the AIT may not list, or
        one of the AIT by its ids (dvb://current.ait/<org>.<app>). False when
        that application is not signalled (the calling app keeps running)."""
        ref = parse_ait_url(url)
        if ref is not None:
            key, extra = ref
            app = self.ait.find(key) if self.ait is not None else None
            base = self.ait.url(app) if app is not None else ""
            if not base:
                return False
            target = append_query(base, extra)
            if self.allowed is None or self.allowed(app):
                self.start(app, target)
            elif self.on_blocked is not None:
                self.on_blocked(app, lambda: self.start(app, target))
            return True
        app = None
        if self.ait is not None:
            app = next((a for a in self.ait.apps if self.ait.url(a) and
                        url.split("?")[0].startswith(self.ait.url(a).split("?")[0])), None)
        self.running = app
        self.running_url = url
        self.launch_cb(url, app)
        return True

    def stop(self, by_user=False):
        if self.running is not None and by_user:
            self.suppressed.add(self.running.key)
        self.running = None
        self.running_url = ""
        self.kill_cb()

    @property
    def active(self):
        return bool(self.running_url)
