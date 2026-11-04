# SPDX-License-Identifier: GPL-2.0-or-later
"""EN 50221 common interface: talk to a CI module (CAM) in the tuner.

This implements the host side of the protocol a CI module speaks over the
Linux CA device (``/dev/dvb/adapterN/caM``):

* the transport layer (TPDUs, polling, chunking),
* the session layer (the module opens sessions to the host's resources),
* the host resources: resource manager, application information, conditional
  access support, date-time and MMI (the module's interactive menus).

The protocol part (:class:`CIStack`) is pure Python and is fed raw frames, so it
can be tested without hardware. :class:`CIDevice` drives it from the kernel CA
device in a background thread.

CI+ host authentication is not implemented: it needs licensed host
certificates. CI+ modules work as far as they fall back to plain CI.
"""

import fcntl
import os
import select
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
#  Length fields (ASN.1 BER style, used in TPDUs, SPDUs and APDUs)
# --------------------------------------------------------------------------


def encode_length(n):
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def decode_length(buf, i):
    """Return ``(length, index after the length field)``."""
    if i >= len(buf):
        raise ValueError("truncated length")
    b = buf[i]
    if b < 0x80:
        return b, i + 1
    n = b & 0x7F
    if i + 1 + n > len(buf):
        raise ValueError("truncated length")
    return int.from_bytes(buf[i + 1:i + 1 + n], "big"), i + 1 + n


# --------------------------------------------------------------------------
#  Constants
# --------------------------------------------------------------------------
# Transport layer tags
T_SB = 0x80
T_RCV = 0x81
T_CREATE_T_C = 0x82
T_C_T_C_REPLY = 0x83
T_DELETE_T_C = 0x84
T_D_T_C_REPLY = 0x85
T_REQUEST_T_C = 0x86
T_NEW_T_C = 0x87
T_T_C_ERROR = 0x77
T_DATA_LAST = 0xA0
T_DATA_MORE = 0xA1

# Session layer tags
ST_SESSION_NUMBER = 0x90
ST_OPEN_SESSION_REQUEST = 0x91
ST_OPEN_SESSION_RESPONSE = 0x92
ST_CREATE_SESSION = 0x93
ST_CREATE_SESSION_RESPONSE = 0x94
ST_CLOSE_SESSION_REQUEST = 0x95
ST_CLOSE_SESSION_RESPONSE = 0x96

SS_OK = 0x00
SS_NOT_ALLOCATED = 0xF0

# Host resources
RI_RESOURCE_MANAGER = 0x00010041
RI_APPLICATION_INFORMATION = 0x00020041
RI_CONDITIONAL_ACCESS_SUPPORT = 0x00030041
RI_DATE_TIME = 0x00240041
RI_MMI = 0x00400041
HOST_RESOURCES = (RI_RESOURCE_MANAGER, RI_APPLICATION_INFORMATION,
                  RI_CONDITIONAL_ACCESS_SUPPORT, RI_DATE_TIME, RI_MMI)

# APDU tags
AOT_PROFILE_ENQ = 0x9F8010
AOT_PROFILE = 0x9F8011
AOT_PROFILE_CHANGE = 0x9F8012
AOT_APPLICATION_INFO_ENQ = 0x9F8020
AOT_APPLICATION_INFO = 0x9F8021
AOT_ENTER_MENU = 0x9F8022
AOT_CA_INFO_ENQ = 0x9F8030
AOT_CA_INFO = 0x9F8031
AOT_CA_PMT = 0x9F8032
AOT_CA_PMT_REPLY = 0x9F8033
AOT_DATE_TIME_ENQ = 0x9F8440
AOT_DATE_TIME = 0x9F8441
AOT_CLOSE_MMI = 0x9F8800
AOT_DISPLAY_CONTROL = 0x9F8801
AOT_DISPLAY_REPLY = 0x9F8802
AOT_TEXT_LAST = 0x9F8803
AOT_TEXT_MORE = 0x9F8804
AOT_KEYPAD_CONTROL = 0x9F8805
AOT_KEYPRESS = 0x9F8806
AOT_ENQ = 0x9F8807
AOT_ANSW = 0x9F8808
AOT_MENU_LAST = 0x9F8809
AOT_MENU_MORE = 0x9F880A
AOT_MENU_ANSW = 0x9F880B
AOT_LIST_LAST = 0x9F880C
AOT_LIST_MORE = 0x9F880D

DCC_SET_MMI_MODE = 0x01
MM_HIGH_LEVEL = 0x01
DRI_MMI_MODE_ACK = 0x01

# Largest TPDU payload the host sends in one go; longer SPDUs are chunked.
MAX_TPDU_DATA = 240
POLL_INTERVAL = 0.1
REPLY_TIMEOUT = 3.0


def decode_dvb_text(data):
    """Decode a string in the DVB character coding (EN 300 468 annex A)."""
    data = bytes(data)
    if not data:
        return ""
    first = data[0]
    if first >= 0x20:
        body = data
        codec = "latin-1"           # table 00 is ISO 6937; latin-1 is close enough
    elif 0x01 <= first <= 0x0B:
        body = data[1:]
        codec = f"iso8859-{first + 4}"
    elif first == 0x10 and len(data) >= 3:
        body = data[3:]
        codec = f"iso8859-{data[2]}" if data[2] else "latin-1"
    elif first == 0x11:
        body, codec = data[1:], "utf-16-be"
    elif first == 0x13:
        body, codec = data[1:], "gb2312"
    elif first == 0x15:
        body, codec = data[1:], "utf-8"
    else:
        body = data[1:]
    try:
        text = body.decode(codec, "replace")
    except LookupError:
        text = body.decode("latin-1", "replace")
    # 0x8A is the DVB line break; drop the other control codes
    text = text.replace("\x8a", "\n")
    return "".join(ch for ch in text if ch == "\n" or not (0x80 <= ord(ch) <= 0x9F))


def apdu(tag, data=b""):
    return tag.to_bytes(3, "big") + encode_length(len(data)) + bytes(data)


def parse_apdus(buf):
    """Yield ``(tag, data)`` for each APDU in ``buf``."""
    i = 0
    while i + 4 <= len(buf):
        tag = int.from_bytes(buf[i:i + 3], "big")
        n, j = decode_length(buf, i + 3)
        yield tag, bytes(buf[j:j + n])
        i = j + n


def mjd_bcd_time(t=None):
    """UTC as the 5-byte MJD + BCD hh:mm:ss used by the date-time resource."""
    t = time.gmtime(time.time() if t is None else t)
    y, m, d = t.tm_year - 1900, t.tm_mon, t.tm_mday
    leap = 1 if m <= 2 else 0
    mjd = 14956 + d + int((y - leap) * 365.25) + int((m + 1 + leap * 12) * 30.6001)

    def bcd(v):
        return ((v // 10) << 4) | (v % 10)
    return struct.pack(">H", mjd) + bytes([bcd(t.tm_hour), bcd(t.tm_min), bcd(t.tm_sec)])


# --------------------------------------------------------------------------
#  MMI objects handed to the user interface
# --------------------------------------------------------------------------
@dataclass
class MMIMenu:
    title: str = ""
    subtitle: str = ""
    bottom: str = ""
    items: list = field(default_factory=list)
    is_list: bool = False       # a list is informational; a menu is selectable


@dataclass
class MMIEnquiry:
    text: str = ""
    blind: bool = False         # hide the typed answer (PIN entry)
    length: int = 0             # expected answer length, 0 = any


def _parse_menu(data, is_list):
    menu = MMIMenu(is_list=is_list)
    texts = [decode_dvb_text(d) for tag, d in parse_apdus(data[1:])
             if tag in (AOT_TEXT_LAST, AOT_TEXT_MORE)]
    texts += [""] * max(0, 3 - len(texts))
    menu.title, menu.subtitle, menu.bottom = texts[:3]
    menu.items = texts[3:]
    return menu


# --------------------------------------------------------------------------
#  Transport layer
# --------------------------------------------------------------------------
class Transport:
    """One EN 50221 transport connection over the Linux CA device.

    Frames written to and read from the device start with the slot and the
    transport connection id, followed by the TPDU.
    """

    def __init__(self, write, slot=0, tcid=1):
        self.write = write
        self.slot = slot
        self.tcid = tcid
        self.active = False
        self.awaiting = False
        self.data_available = False
        self.last_sent = 0.0
        self.out = deque()          # pending (tag, data) TPDUs
        self._rx = bytearray()

    def reset(self):
        self.active = False
        self.awaiting = False
        self.data_available = False
        self.out.clear()
        self._rx = bytearray()

    def start(self, now=None):
        self.reset()
        self._send(T_CREATE_T_C, b"", now)

    def queue_spdu(self, spdu):
        for i in range(0, max(1, len(spdu)), MAX_TPDU_DATA):
            chunk = spdu[i:i + MAX_TPDU_DATA]
            last = i + MAX_TPDU_DATA >= len(spdu)
            self.out.append((T_DATA_LAST if last else T_DATA_MORE, chunk))

    def _send(self, tag, data, now=None):
        body = bytes([self.tcid]) + bytes(data)
        self.write(bytes([self.slot, self.tcid, tag]) + encode_length(len(body)) + body)
        self.awaiting = True
        self.last_sent = time.monotonic() if now is None else now

    def feed(self, frame):
        """Handle one frame read from the device. Returns complete SPDUs."""
        spdus = []
        if len(frame) < 3:
            return spdus
        buf = frame[2:]             # strip slot / tcid of the link layer
        i = 0
        while i < len(buf):
            tag = buf[i]
            try:
                n, j = decode_length(buf, i + 1)
            except ValueError:
                break
            body = buf[j:j + n]
            i = j + n
            data = body[1:]         # first byte of every TPDU body is the tcid
            if tag == T_C_T_C_REPLY:
                self.active = True
            elif tag in (T_DATA_LAST, T_DATA_MORE):
                self._rx += data
                if tag == T_DATA_LAST:
                    if self._rx:
                        spdus.append(bytes(self._rx))
                    self._rx = bytearray()
            elif tag == T_SB:
                self.data_available = bool(data and data[0] & 0x80)
                self.awaiting = False
            elif tag == T_DELETE_T_C:
                self.reset()
                self._send(T_D_T_C_REPLY, b"")
                self.awaiting = False
            elif tag == T_REQUEST_T_C:
                # we only ever use one transport connection
                self._send(T_T_C_ERROR, b"\x01")
                self.awaiting = False
        return spdus

    def poll(self, now=None):
        now = time.monotonic() if now is None else now
        if self.awaiting:
            if now - self.last_sent > REPLY_TIMEOUT:
                self.awaiting = False       # module did not answer; try again
            return
        if not self.active:
            return
        if self.data_available:
            self._send(T_RCV, b"", now)
        elif self.out:
            tag, data = self.out.popleft()
            self._send(tag, data, now)
        elif now - self.last_sent >= POLL_INTERVAL:
            self._send(T_DATA_LAST, b"", now)


# --------------------------------------------------------------------------
#  Session layer and host resources
# --------------------------------------------------------------------------
class CIListener:
    """Callbacks from the CI stack. They run on the CI thread."""

    def ci_info(self, stack):
        """Module name or CA systems changed."""

    def ci_menu(self, stack, menu):
        """The module shows a menu or list (:class:`MMIMenu`)."""

    def ci_enquiry(self, stack, enquiry):
        """The module asks for input (:class:`MMIEnquiry`)."""

    def ci_close(self, stack):
        """The module closed its menu."""

    def ci_removed(self, stack):
        """The module was removed or reset."""


class CIStack:
    """Host side of the EN 50221 session and application layers."""

    def __init__(self, write, slot=0):
        self.transport = Transport(write, slot)
        self.sessions = {}              # session number -> resource id
        self._next_session = 1
        self.listeners = []
        self.module_name = ""
        self.manufacturer = 0
        self.ca_system_ids = []
        self.mmi_open = False
        self._pending_ca_pmt = None
        self._dt_interval = 0
        self._dt_next = 0.0
        self.started = False

    # -- life cycle -----------------------------------------------------------
    def start(self, now=None):
        self.sessions.clear()
        self._next_session = 1
        self.module_name = ""
        self.ca_system_ids = []
        self.mmi_open = False
        self._dt_interval = 0
        self.started = True
        self.transport.start(now)

    def stop(self):
        was = self.started
        self.started = False
        self.transport.reset()
        self.sessions.clear()
        self.mmi_open = False
        if was:
            self._notify("ci_removed")

    def feed(self, frame):
        for spdu in self.transport.feed(frame):
            self._spdu(spdu)

    def poll(self, now=None):
        now = time.monotonic() if now is None else now
        if self._dt_interval and now >= self._dt_next:
            nb = self._session_for(RI_DATE_TIME)
            if nb:
                self._send_apdu(nb, AOT_DATE_TIME, mjd_bcd_time())
            self._dt_next = now + self._dt_interval
        self.transport.poll(now)

    # -- user commands --------------------------------------------------------
    def enter_menu(self):
        """Ask the module to open its main menu."""
        nb = self._session_for(RI_APPLICATION_INFORMATION)
        if not nb:
            return False
        self._send_apdu(nb, AOT_ENTER_MENU)
        return True

    def answer_menu(self, choice):
        """Pick menu item ``choice`` (1-based); 0 goes back / closes."""
        nb = self._session_for(RI_MMI)
        if nb:
            self._send_apdu(nb, AOT_MENU_ANSW, bytes([choice & 0xFF]))

    def answer_enquiry(self, text):
        """Answer an enquiry; ``None`` cancels."""
        nb = self._session_for(RI_MMI)
        if not nb:
            return
        if text is None:
            self._send_apdu(nb, AOT_ANSW, b"\x00")
        else:
            self._send_apdu(nb, AOT_ANSW, b"\x01" + text.encode("latin-1", "replace"))

    def close_mmi(self):
        nb = self._session_for(RI_MMI)
        if nb:
            self._send_apdu(nb, AOT_CLOSE_MMI, b"\x00")
        self.mmi_open = False

    def send_ca_pmt(self, ca_pmt_body):
        """Send an EN 50221 CA_PMT object body to the module."""
        nb = self._session_for(RI_CONDITIONAL_ACCESS_SUPPORT)
        if nb and self.ca_system_ids:
            self._send_apdu(nb, AOT_CA_PMT, ca_pmt_body)
            self._pending_ca_pmt = None
        else:
            self._pending_ca_pmt = ca_pmt_body

    # -- internals ------------------------------------------------------------
    def _notify(self, name, *args):
        for listener in list(self.listeners):
            try:
                getattr(listener, name)(self, *args)
            except Exception:
                pass

    def _session_for(self, resource):
        mask = 0xFFFFFFC0
        for nb, rid in self.sessions.items():
            if rid & mask == resource & mask:
                return nb
        return 0

    def _send_spdu(self, spdu):
        self.transport.queue_spdu(spdu)

    def _send_apdu(self, nb, tag, data=b""):
        self._send_spdu(bytes([ST_SESSION_NUMBER, 0x02]) + struct.pack(">H", nb) + apdu(tag, data))

    def _spdu(self, spdu):
        tag = spdu[0]
        try:
            n, i = decode_length(spdu, 1)
        except ValueError:
            return
        body = spdu[i:i + n]
        rest = spdu[i + n:]
        if tag == ST_OPEN_SESSION_REQUEST and len(body) >= 4:
            rid = int.from_bytes(body[:4], "big")
            host = next((r for r in HOST_RESOURCES if r & 0xFFFFFFC0 == rid & 0xFFFFFFC0), None)
            if host is None:
                self._send_spdu(bytes([ST_OPEN_SESSION_RESPONSE, 0x07, SS_NOT_ALLOCATED])
                                + body[:4] + b"\x00\x00")
                return
            nb = self._next_session
            self._next_session += 1
            self.sessions[nb] = host
            self._send_spdu(bytes([ST_OPEN_SESSION_RESPONSE, 0x07, SS_OK])
                            + host.to_bytes(4, "big") + struct.pack(">H", nb))
            self._opened(nb, host)
        elif tag == ST_CLOSE_SESSION_REQUEST and len(body) >= 2:
            nb = struct.unpack(">H", body[:2])[0]
            rid = self.sessions.pop(nb, None)
            self._send_spdu(bytes([ST_CLOSE_SESSION_RESPONSE, 0x03, SS_OK]) + body[:2])
            if rid is not None and rid & 0xFFFFFFC0 == RI_MMI & 0xFFFFFFC0 and self.mmi_open:
                self.mmi_open = False
                self._notify("ci_close")
        elif tag == ST_SESSION_NUMBER and len(body) >= 2:
            nb = struct.unpack(">H", body[:2])[0]
            for atag, data in parse_apdus(rest):
                self._apdu(nb, atag, data)

    def _opened(self, nb, rid):
        if rid == RI_RESOURCE_MANAGER:
            self._send_apdu(nb, AOT_PROFILE_ENQ)
        elif rid == RI_APPLICATION_INFORMATION:
            self._send_apdu(nb, AOT_APPLICATION_INFO_ENQ)
        elif rid == RI_CONDITIONAL_ACCESS_SUPPORT:
            self._send_apdu(nb, AOT_CA_INFO_ENQ)

    def _apdu(self, nb, tag, data):
        rid = self.sessions.get(nb)
        if rid is None:
            return
        if tag == AOT_PROFILE_ENQ:
            self._send_apdu(nb, AOT_PROFILE, b"".join(r.to_bytes(4, "big") for r in HOST_RESOURCES))
        elif tag == AOT_PROFILE:
            self._send_apdu(nb, AOT_PROFILE_CHANGE)
        elif tag == AOT_APPLICATION_INFO and len(data) >= 6:
            self.manufacturer = struct.unpack(">H", data[1:3])[0]
            n = data[5]
            self.module_name = decode_dvb_text(data[6:6 + n])
            self._notify("ci_info")
        elif tag == AOT_CA_INFO:
            self.ca_system_ids = [struct.unpack(">H", data[k:k + 2])[0]
                                  for k in range(0, len(data) - 1, 2)]
            self._notify("ci_info")
            if self._pending_ca_pmt is not None:
                self.send_ca_pmt(self._pending_ca_pmt)
        elif tag == AOT_DATE_TIME_ENQ:
            self._dt_interval = data[0] if data else 0
            self._send_apdu(nb, AOT_DATE_TIME, mjd_bcd_time())
            self._dt_next = time.monotonic() + self._dt_interval
        elif tag == AOT_DISPLAY_CONTROL:
            if data and data[0] == DCC_SET_MMI_MODE:
                self._send_apdu(nb, AOT_DISPLAY_REPLY, bytes([DRI_MMI_MODE_ACK, MM_HIGH_LEVEL]))
            self.mmi_open = True
        elif tag in (AOT_MENU_LAST, AOT_MENU_MORE, AOT_LIST_LAST, AOT_LIST_MORE):
            self.mmi_open = True
            self._notify("ci_menu", _parse_menu(data, tag in (AOT_LIST_LAST, AOT_LIST_MORE)))
        elif tag == AOT_ENQ and len(data) >= 2:
            self.mmi_open = True
            self._notify("ci_enquiry", MMIEnquiry(decode_dvb_text(data[2:]),
                                                  bool(data[0] & 0x01), data[1]))
        elif tag == AOT_CLOSE_MMI:
            self.mmi_open = False
            self._notify("ci_close")


# --------------------------------------------------------------------------
#  Linux CA device driver
# --------------------------------------------------------------------------
CA_RESET = 0x6F80
CA_GET_SLOT_INFO = 0x800C6F82
CA_CI_LINK = 2
CA_CI_MODULE_PRESENT = 1
CA_CI_MODULE_READY = 2


class CIDevice:
    """Runs a :class:`CIStack` against ``/dev/dvb/adapterN/caM``."""

    def __init__(self, path, slot=0):
        self.path = path
        self.slot = slot
        self.fd = None
        self.stack = CIStack(self._write, slot)
        self.state = "closed"
        self._thread = None
        self._running = False
        self._lock = threading.Lock()

    @property
    def listeners(self):
        return self.stack.listeners

    def open(self):
        if self.fd is not None:
            return True
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            self.state = f"cannot open {self.path}: {e.strerror}"
            return False
        try:
            fcntl.ioctl(self.fd, CA_RESET, 1 << self.slot)
        except OSError:
            pass
        self.state = "waiting for module"
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="ci")
        self._thread.start()
        return True

    def close(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)
            self._thread = None
        self.stack.stop()
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        self.state = "closed"

    def _write(self, frame):
        if self.fd is None:
            return
        try:
            os.write(self.fd, frame)
        except OSError:
            pass

    def slot_flags(self):
        buf = bytearray(struct.pack("iiI", self.slot, 0, 0))
        try:
            fcntl.ioctl(self.fd, CA_GET_SLOT_INFO, buf, True)
        except OSError:
            return None, 0
        _num, typ, flags = struct.unpack("iiI", bytes(buf))
        return typ, flags

    def _run(self):
        next_check = 0.0
        while self._running and self.fd is not None:
            now = time.monotonic()
            if now >= next_check:
                next_check = now + 1.0
                typ, flags = self.slot_flags()
                ready = bool(flags & CA_CI_MODULE_READY)
                if ready and not self.stack.started:
                    with self._lock:
                        self.stack.start(now)
                    self.state = "module ready"
                elif not ready and self.stack.started:
                    with self._lock:
                        self.stack.stop()
                    self.state = "no module" if not flags & CA_CI_MODULE_PRESENT else \
                        "module initialising"
                elif not ready:
                    self.state = "no module" if not flags & CA_CI_MODULE_PRESENT else \
                        "module initialising"
            try:
                r, _, _ = select.select([self.fd], [], [], POLL_INTERVAL / 2)
            except (OSError, ValueError):
                break
            if r:
                try:
                    frame = os.read(self.fd, 4096)
                except BlockingIOError:
                    frame = b""
                except OSError:
                    frame = b""
                if frame:
                    with self._lock:
                        self.stack.feed(frame)
            if self.stack.started:
                with self._lock:
                    self.stack.poll()

    # thread-safe wrappers for the UI and the CA provider
    def call(self, fn, *args):
        with self._lock:
            return fn(*args)

    def enter_menu(self):
        return self.call(self.stack.enter_menu)

    def answer_menu(self, choice):
        self.call(self.stack.answer_menu, choice)

    def answer_enquiry(self, text):
        self.call(self.stack.answer_enquiry, text)

    def close_mmi(self):
        self.call(self.stack.close_mmi)

    def send_ca_pmt(self, body):
        self.call(self.stack.send_ca_pmt, body)


_devices = {}


def shared_device(settings):
    """The one CIDevice for the configured CA device, created on first use."""
    path = f"/dev/dvb/adapter{settings.get('ca_ci_adapter', 0)}/ca{settings.get('ca_ci_device', 0)}"
    dev = _devices.get(path)
    if dev is None:
        dev = _devices[path] = CIDevice(path)
    return dev


def close_all():
    for dev in _devices.values():
        dev.close()
    _devices.clear()
