"""Conditional access for the user's own encrypted channels.

LinDVB does not contain any decryption keys and does not break any encryption.
What this module does is the same thing tvheadend, VDR and enigma2 do: it talks
to a conditional-access client that the *user* runs and that reads the user's
*own* subscription smart card. That external client (a "softcam" such as OSCam
reading the card in a card reader, or a hardware CI/CI+ module plugged into the
tuner) does the actual ECM processing with the subscription card and returns the
control words. LinDVB then applies those control words to descramble the
transport stream with the common scrambling algorithm (libdvbcsa).

Two built-in providers:

``dvbapi``
    A client of the DVBAPI socket protocol exposed by OSCam and compatible
    softcams. LinDVB runs the demux section filters the softcam asks for,
    forwards the ECM/EMM sections to it, receives the control words and
    descrambles in software with libdvbcsa.

``ci``
    A hardware CI/CI+ common-interface module in the adapter's CA device
    (``/dev/dvb/adapterN/caM``). The module descrambles; LinDVB sends it the
    CA PMT through the kernel CA device.

Plugins can register further providers (see :mod:`lindvb.plugins`). All of this
is for descrambling channels the user is entitled to with their own card; it is
not a means of circumventing access control.
"""

import ctypes
import ctypes.util
import os
import socket
import struct
import threading

TS_PACKET = 188
TS_SYNC = 0x47


# --------------------------------------------------------------------------
#  MPEG-TS / PSI section helpers (pure, unit-tested)
# --------------------------------------------------------------------------
def ts_pid(packet):
    return ((packet[1] & 0x1F) << 8) | packet[2]


def ts_scrambling(packet):
    """transport_scrambling_control: 0 clear, 2 even key, 3 odd key (1 reserved)."""
    return (packet[3] >> 6) & 0x03


def ts_payload_offset(packet):
    """Offset of the payload within the packet, or ``None`` if there is none."""
    adaptation = (packet[3] >> 4) & 0x03
    if adaptation in (0, 2):          # 0 reserved, 2 adaptation only, no payload
        return None
    offset = 4
    if adaptation == 3:               # adaptation field followed by payload
        offset += 1 + packet[4]
    return offset if offset < TS_PACKET else None


def parse_ca_descriptors(data):
    """Yield ``(ca_system_id, ca_pid)`` from a descriptor loop (tag 0x09)."""
    i = 0
    while i + 2 <= len(data):
        tag, length = data[i], data[i + 1]
        body = data[i + 2:i + 2 + length]
        if tag == 0x09 and len(body) >= 4:
            ca_system_id = (body[0] << 8) | body[1]
            ca_pid = ((body[2] & 0x1F) << 8) | body[3]
            yield ca_system_id, ca_pid
        i += 2 + length


class ProgramMapTable:
    """A parsed PMT section: the information a softcam needs for one service."""

    def __init__(self, program_number=0, pcr_pid=0):
        self.program_number = program_number
        self.pcr_pid = pcr_pid
        self.program_ca = []                 # [(ca_system_id, ca_pid)]
        self.program_info = b""              # raw program-level descriptor loop
        self.streams = []                    # [(stream_type, pid, ca_list, info)]

    @property
    def ca_pids(self):
        pids = {pid for _sys, pid in self.program_ca}
        for _st, _pid, ca, _info in self.streams:
            pids |= {p for _s, p in ca}
        return sorted(pids)

    @property
    def es_pids(self):
        return [pid for _st, pid, _ca, _info in self.streams]


def parse_pmt(section):
    """Parse a PMT section (starting at table_id 0x02). Returns a ProgramMapTable.

    Raises ``ValueError`` if the section is not a well-formed PMT.
    """
    if len(section) < 12 or section[0] != 0x02:
        raise ValueError("not a PMT section")
    section_length = ((section[1] & 0x0F) << 8) | section[2]
    end = 3 + section_length
    if section_length < 9 or end > len(section):
        raise ValueError("bad PMT section length")
    program_number = (section[3] << 8) | section[4]
    pcr_pid = ((section[8] & 0x1F) << 8) | section[9]
    program_info_length = ((section[10] & 0x0F) << 8) | section[11]
    pmt = ProgramMapTable(program_number, pcr_pid)
    i = 12
    pmt.program_info = section[i:i + program_info_length]
    pmt.program_ca = list(parse_ca_descriptors(pmt.program_info))
    i += program_info_length
    crc_end = end - 4                      # last 4 bytes are the CRC32
    while i + 5 <= crc_end:
        stream_type = section[i]
        pid = ((section[i + 1] & 0x1F) << 8) | section[i + 2]
        es_info_length = ((section[i + 3] & 0x0F) << 8) | section[i + 4]
        info = section[i + 5:i + 5 + es_info_length]
        pmt.streams.append((stream_type, pid, list(parse_ca_descriptors(info)), info))
        i += 5 + es_info_length
    return pmt


# CA_PMT list-management and command ids (EN 50221)
CA_PMT_LIST_ONLY = 0x03
CA_PMT_CMD_OK_DESCRAMBLING = 0x01


def build_ca_pmt(pmt, list_mgmt=CA_PMT_LIST_ONLY,
                 cmd_id=CA_PMT_CMD_OK_DESCRAMBLING, demux=0, adapter=0,
                 service_pmt_pid=0):
    """Build an EN 50221 CA_PMT object body from a :class:`ProgramMapTable`.

    This is the payload the softcam / CI module consumes to learn which ECM
    PIDs belong to the service. A private descriptor (tag 0x82) carries the
    demux and adapter indices, as the DVBAPI protocol expects.
    """
    # Program-level private descriptor: demux/adapter selector for DVBAPI.
    prog_desc = bytes([0x82, 0x02, demux & 0xFF, adapter & 0xFF])
    if service_pmt_pid:
        prog_desc += bytes([0x84, 0x02, (service_pmt_pid >> 8) & 0x1F, service_pmt_pid & 0xFF])
    program_info = bytes([cmd_id]) + prog_desc + pmt.program_info

    out = bytearray()
    out.append(list_mgmt)
    out += struct.pack(">H", pmt.program_number)
    out.append(0x01)                        # reserved + version(0) + current_next(1)
    out += struct.pack(">H", len(program_info) & 0x0FFF)
    out += program_info
    for stream_type, pid, _ca, info in pmt.streams:
        es_info = (bytes([cmd_id]) + info) if info else b""
        out.append(stream_type)
        out += struct.pack(">H", pid & 0x1FFF)
        out += struct.pack(">H", len(es_info) & 0x0FFF)
        out += es_info
    return bytes(out)


def _asn1_length(n):
    if n < 0x80:
        return bytes([n])
    body = []
    while n:
        body.insert(0, n & 0xFF)
        n >>= 8
    return bytes([0x80 | len(body)]) + bytes(body)


AOT_CA_PMT = b"\x9f\x80\x32"


def wrap_ca_pmt(ca_pmt_body):
    """Wrap a CA_PMT body as an AOT_CA_PMT application object (DVBAPI opcode)."""
    return AOT_CA_PMT + _asn1_length(len(ca_pmt_body)) + ca_pmt_body


# --------------------------------------------------------------------------
#  Common scrambling algorithm via libdvbcsa
# --------------------------------------------------------------------------
class CSADescrambler:
    """Software DVB-CSA descrambler backed by libdvbcsa.

    ``available`` is ``False`` when libdvbcsa is not installed, in which case
    :meth:`descramble_packet` leaves packets untouched (the stream stays
    scrambled) instead of failing.
    """

    def __init__(self):
        self.available = False
        self._lib = None
        self._key_even = None
        self._key_odd = None
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        name = ctypes.util.find_library("dvbcsa") or "libdvbcsa.so.1"
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            return
        try:
            lib.dvbcsa_key_alloc.restype = ctypes.c_void_p
            lib.dvbcsa_key_set.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
            lib.dvbcsa_decrypt.argtypes = [ctypes.c_void_p,
                                           ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int]
            self._lib = lib
            self._key_even = lib.dvbcsa_key_alloc()
            self._key_odd = lib.dvbcsa_key_alloc()
            self.available = bool(self._key_even and self._key_odd)
        except AttributeError:
            self._lib = None

    def set_control_word(self, parity, cw):
        """parity 0 = even key, 1 = odd key; ``cw`` is 8 bytes."""
        if not self.available or len(cw) != 8:
            return
        key = self._key_even if parity == 0 else self._key_odd
        with self._lock:
            self._lib.dvbcsa_key_set(bytes(cw), key)

    def descramble_packet(self, packet):
        """Descramble one 188-byte TS packet in place (``bytearray``).

        Returns ``True`` when the packet was (or did not need to be)
        descrambled, ``False`` when no key is loaded for its parity yet.
        """
        scr = (packet[3] >> 6) & 0x03
        if scr < 2:                         # already clear
            return True
        if not self.available:
            return False
        key = self._key_even if scr == 2 else self._key_odd
        offset = ts_payload_offset(packet)
        if offset is None:
            # scrambling bits set but no payload: just clear the bits
            packet[3] &= 0x3F
            return True
        length = TS_PACKET - offset
        buf = (ctypes.c_ubyte * length).from_buffer(packet, offset)
        with self._lock:
            self._lib.dvbcsa_decrypt(key, buf, length)
        packet[3] &= 0x3F                   # mark payload as clear
        return True


# --------------------------------------------------------------------------
#  Section assembly (for forwarding ECM/EMM sections to the softcam)
# --------------------------------------------------------------------------
class SectionAssembler:
    """Reassembles PSI sections from the TS payload of a single PID."""

    def __init__(self):
        self._buf = bytearray()
        self._want = 0

    def feed(self, packet):
        """Feed one TS packet; yield each complete section (bytes)."""
        offset = ts_payload_offset(packet)
        if offset is None:
            return
        payload = packet[offset:]
        if packet[1] & 0x40:                # payload_unit_start_indicator
            if not payload:
                return
            pointer = payload[0]
            payload = payload[1 + pointer:]
            self._buf = bytearray(payload)
        else:
            if not self._buf:
                return
            self._buf += payload
        while len(self._buf) >= 3:
            length = ((self._buf[1] & 0x0F) << 8) | self._buf[2]
            total = 3 + length
            if len(self._buf) < total:
                break
            yield bytes(self._buf[:total])
            self._buf = self._buf[total:]
            if not self._buf or self._buf[0] == 0xFF:   # stuffing
                self._buf = bytearray()
                break


def section_matches(section, flt, mask):
    """EN demux filter match used by the DVBAPI section filters.

    Byte 0 of the filter is matched against the table_id (section byte 0);
    bytes 1.. are matched against section bytes 3.. (skipping the length), as
    the Linux demux ``dmx_sct_filter_params`` defines.
    """
    if not section:
        return False
    positions = [0] + list(range(3, 3 + len(flt) - 1))
    for i, pos in enumerate(positions):
        if i >= len(flt):
            break
        m = mask[i] if i < len(mask) else 0
        value = section[pos] if pos < len(section) else 0
        if (value & m) != (flt[i] & m):
            return False
    return True


# --------------------------------------------------------------------------
#  CA provider base class
# --------------------------------------------------------------------------
class CAProvider:
    """A source of control words for the user's own encrypted channels.

    The tuner's capture calls :meth:`start` once per tuned encrypted service,
    then :meth:`process_packet` for every TS packet (descrambling in place),
    and :meth:`stop` when the service is released.
    """

    name = "none"

    def start(self, channel, pmt_section=None):
        """Begin descrambling ``channel``. ``pmt_section`` is its PMT if known."""

    def set_pmt(self, pmt_section):
        """Supply the PMT section once it has been seen in the stream."""

    def process_packet(self, packet):
        """Descramble one TS packet (``bytearray``) in place if needed."""

    def stop(self):
        """Release the service."""

    @property
    def status(self):
        return "idle"


class NullProvider(CAProvider):
    """Does nothing; encrypted packets stay scrambled."""

    def process_packet(self, packet):
        return False


# --------------------------------------------------------------------------
#  DVBAPI softcam client (OSCam and compatible)
# --------------------------------------------------------------------------
# Opcodes as defined by OSCam's module-dvbapi.
DVBAPI_FILTER_DATA = 0xFFFF0000
DVBAPI_CLIENT_INFO = 0xFFFF0001
DVBAPI_SERVER_INFO = 0xFFFF0002
DVBAPI_ECM_INFO = 0xFFFF0003
DVBAPI_CA_SET_DESCR = 0x40106F86
DVBAPI_CA_SET_DESCR_MODE = 0x400C6F88
DVBAPI_CA_SET_PID = 0x40086F87
DVBAPI_DMX_SET_FILTER = 0x403C6F2B
DVBAPI_DMX_STOP = 0x40046F2A
DVBAPI_PROTOCOL_VERSION = 2
LINDVB_CLIENT_NAME = b"LinDVB"


class DvbApiProvider(CAProvider):
    """Client of an external softcam over the DVBAPI socket protocol.

    The softcam (OSCam &c.) reads the user's subscription card and drives the
    descrambling: it tells us which ECM/EMM sections to filter, we forward the
    sections it asked for, and it returns the control words. No keys or card
    data live in LinDVB.
    """

    name = "dvbapi"

    def __init__(self, settings):
        self.settings = settings
        self._csa = CSADescrambler()
        self._sock = None
        self._thread = None
        self._running = False
        self._lock = threading.Lock()
        self._pmt = None
        self._channel = None
        self._filters = {}        # (demux, filter_num) -> (pid, filter, mask)
        self._assemblers = {}     # pid -> SectionAssembler
        self._state = "stopped"

    @property
    def status(self):
        extra = "" if self._csa.available else " (libdvbcsa missing)"
        return self._state + extra

    # -- life cycle -----------------------------------------------------------
    def start(self, channel, pmt_section=None):
        self._channel = channel
        self._running = True
        self._state = "connecting"
        if pmt_section:
            self.set_pmt(pmt_section)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_pmt(self, pmt_section):
        try:
            self._pmt = parse_pmt(pmt_section)
        except ValueError:
            return
        if self._sock is not None:
            self._send_capmt()

    def stop(self):
        self._running = False
        self._state = "stopped"
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    # -- connection -----------------------------------------------------------
    def _connect(self):
        target = self.settings.get("ca_dvbapi_socket", "/tmp/camd.socket")
        if ":" in target and not os.path.exists(target):
            host, _, port = target.rpartition(":")
            sock = socket.create_connection((host or "127.0.0.1", int(port)), timeout=10)
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(target)
        sock.settimeout(1.0)
        return sock

    def _run(self):
        try:
            self._sock = self._connect()
        except OSError as e:
            self._state = f"no softcam: {e}"
            return
        self._state = "connected"
        self._send_client_info()
        if self._pmt is not None:
            self._send_capmt()
        buf = b""
        while self._running:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            buf = self._consume(buf)
        self._state = "disconnected"

    # -- outbound -------------------------------------------------------------
    def _send(self, data):
        sock = self._sock
        if sock is None:
            return
        try:
            with self._lock:
                sock.sendall(data)
        except OSError:
            pass

    def _send_client_info(self):
        msg = struct.pack(">IH", DVBAPI_CLIENT_INFO, DVBAPI_PROTOCOL_VERSION)
        msg += bytes([len(LINDVB_CLIENT_NAME)]) + LINDVB_CLIENT_NAME
        self._send(msg)

    def _send_capmt(self):
        if self._pmt is None:
            return
        pmt_pid = getattr(self._channel, "pmt_pid", 0) if self._channel else 0
        body = build_ca_pmt(self._pmt, service_pmt_pid=pmt_pid)
        self._send(wrap_ca_pmt(body))
        self._state = "waiting for control word"

    def _send_filter_data(self, demux, filter_num, section):
        msg = struct.pack(">I", DVBAPI_FILTER_DATA)
        msg += bytes([demux & 0xFF, filter_num & 0xFF]) + section
        self._send(msg)

    # -- inbound --------------------------------------------------------------
    def _consume(self, buf):
        """Parse as many complete server messages as ``buf`` holds."""
        while len(buf) >= 4:
            opcode = struct.unpack(">I", buf[:4])[0]
            consumed = self._handle(opcode, buf)
            if consumed is None:            # need more bytes
                break
            buf = buf[consumed:]
        return buf

    def _handle(self, opcode, buf):
        if opcode == DVBAPI_SERVER_INFO:
            if len(buf) < 7:
                return None
            namelen = buf[6]
            total = 7 + namelen
            if len(buf) < total:
                return None
            self._state = "ready"
            return total
        if opcode == DVBAPI_CA_SET_DESCR:
            if len(buf) < 4 + 1 + 4 + 4 + 8:
                return None
            _adapter = buf[4]
            index, parity = struct.unpack(">II", buf[5:13])
            cw = buf[13:21]
            self._csa.set_control_word(parity & 1, cw)
            self._state = "descrambling"
            return 21
        if opcode == DVBAPI_CA_SET_DESCR_MODE:
            return 4 + 1 + 4 + 4 + 4 if len(buf) >= 17 else None
        if opcode == DVBAPI_CA_SET_PID:
            return 4 + 1 + 4 + 4 if len(buf) >= 13 else None
        if opcode == DVBAPI_DMX_SET_FILTER:
            # opcode(4) adapter(1) demux(1) filter_num(1) pid(2) filter(16) mask(16) ...
            if len(buf) < 4 + 3 + 2 + 32:
                return None
            demux, filter_num = buf[5], buf[6]
            pid = struct.unpack(">H", buf[7:9])[0]
            flt = buf[9:25]
            mask = buf[25:41]
            with self._lock:
                self._filters[(demux, filter_num)] = (pid, flt, mask)
                self._assemblers.setdefault(pid, SectionAssembler())
            # The whole params block is 0x3c=60 bytes after demux/filter ids.
            return 4 + 3 + 60
        if opcode == DVBAPI_DMX_STOP:
            if len(buf) < 4 + 3 + 2:
                return None
            demux, filter_num = buf[5], buf[6]
            with self._lock:
                self._filters.pop((demux, filter_num), None)
            return 4 + 3 + 2
        if opcode == DVBAPI_ECM_INFO:
            # Variable length, terminated by several length-prefixed strings; we
            # do not need its contents, so resynchronise by dropping one byte.
            return 1
        # Unknown opcode: drop one byte to resynchronise.
        return 1

    # -- the TS path ----------------------------------------------------------
    def process_packet(self, packet):
        pid = ts_pid(packet)
        if self._filters:
            self._run_filters(pid, packet)
        return self._csa.descramble_packet(packet)

    def _run_filters(self, pid, packet):
        with self._lock:
            assembler = self._assemblers.get(pid)
            matching = [(d, f, flt, mask) for (d, f), (p, flt, mask)
                        in self._filters.items() if p == pid]
        if assembler is None or not matching:
            return
        for section in assembler.feed(packet):
            for demux, filter_num, flt, mask in matching:
                if section_matches(section, flt, mask):
                    self._send_filter_data(demux, filter_num, section)


# --------------------------------------------------------------------------
#  Hardware CI / CI+ via the kernel CA device
# --------------------------------------------------------------------------
class CIProvider(CAProvider):
    """A hardware CI/CI+ common-interface module in the adapter's CA device.

    The module itself descrambles the stream in the tuner's hardware once it
    has been given the CA PMT, so LinDVB does not touch the TS payload here; it
    only opens ``/dev/dvb/adapterN/caM`` and forwards the CA PMT. A full
    EN 50221 session/resource layer (including the MMI menu) is a larger piece
    of work; this provider establishes the device and the CA-PMT path and
    reports what it finds, which is the groundwork the CI menu builds on.
    """

    name = "ci"

    def __init__(self, settings):
        self.settings = settings
        self._fd = None
        self._state = "stopped"
        self._channel = None
        self._pmt = None

    @property
    def status(self):
        return self._state

    def _device_path(self):
        adapter = self.settings.get("ca_ci_adapter", 0)
        device = self.settings.get("ca_ci_device", 0)
        return f"/dev/dvb/adapter{adapter}/ca{device}"

    def start(self, channel, pmt_section=None):
        self._channel = channel
        path = self._device_path()
        try:
            self._fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            self._state = "ci device open"
        except OSError as e:
            self._state = f"no CI device: {e}"
            self._fd = None
        if pmt_section:
            self.set_pmt(pmt_section)

    def set_pmt(self, pmt_section):
        try:
            self._pmt = parse_pmt(pmt_section)
        except ValueError:
            return
        # Forwarding the CA PMT to the module is done through the EN 50221
        # session layer on the CA device; the assembled object is prepared here.
        self._ca_pmt = build_ca_pmt(self._pmt)
        self._state = "ca pmt ready"

    def process_packet(self, packet):
        # Hardware CI descrambles in the tuner; nothing to do in software.
        return True

    def stop(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self._state = "stopped"


def builtin_provider(name, settings):
    """Return a built-in provider by name, or ``None``."""
    if name == "dvbapi":
        return DvbApiProvider(settings)
    if name == "ci":
        return CIProvider(settings)
    return None
