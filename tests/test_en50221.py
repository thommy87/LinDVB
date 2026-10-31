"""EN 50221 host stack against a simulated CI module."""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from lindvb import en50221 as ci  # noqa: E402


def text(s):
    return ci.apdu(ci.AOT_TEXT_LAST, s.encode("latin-1"))


class FakeModule:
    """Module side of the protocol: answers TPDUs and queues SPDUs for the host."""

    def __init__(self):
        self.to_host = []           # frames for the host
        self.outbox = []            # SPDUs waiting to be fetched by the host
        self.received = []          # (session, tag, data) APDUs from the host
        self.sessions = {}          # resource -> session number
        self.rx = bytearray()

    # the host's write() lands here
    def write(self, frame):
        slot, tcid, tag = frame[0], frame[1], frame[2]
        n, i = ci.decode_length(frame, 3)
        data = frame[i + 1:i + n]
        reply = b""
        if tag == ci.T_CREATE_T_C:
            reply += bytes([ci.T_C_T_C_REPLY, 1, tcid])
        elif tag in (ci.T_DATA_LAST, ci.T_DATA_MORE):
            self.rx += data
            if tag == ci.T_DATA_LAST and self.rx:
                self._spdu(bytes(self.rx))
                self.rx = bytearray()
        elif tag == ci.T_RCV:
            spdu = self.outbox.pop(0)
            reply += bytes([ci.T_DATA_LAST]) + ci.encode_length(len(spdu) + 1) + bytes([tcid]) + spdu
        sb = 0x80 if self.outbox else 0x00
        reply += bytes([ci.T_SB, 2, tcid, sb])
        self.to_host.append(bytes([slot, tcid]) + reply)

    def _spdu(self, spdu):
        tag = spdu[0]
        if tag == ci.ST_OPEN_SESSION_RESPONSE:
            status = spdu[2]
            rid = int.from_bytes(spdu[3:7], "big")
            nb = struct.unpack(">H", spdu[7:9])[0]
            if status == ci.SS_OK:
                self.sessions[rid] = nb
        elif tag == ci.ST_SESSION_NUMBER:
            nb = struct.unpack(">H", spdu[2:4])[0]
            for atag, data in ci.parse_apdus(spdu[4:]):
                self.received.append((nb, atag, data))

    def open_session(self, rid):
        self.outbox.append(bytes([ci.ST_OPEN_SESSION_REQUEST, 4]) + rid.to_bytes(4, "big"))

    def send_apdu(self, rid, tag, data=b""):
        nb = self.sessions[rid]
        self.outbox.append(bytes([ci.ST_SESSION_NUMBER, 2]) + struct.pack(">H", nb)
                           + ci.apdu(tag, data))

    def got(self, tag):
        return [d for _nb, t, d in self.received if t == tag]


class Recorder(ci.CIListener):
    def __init__(self):
        self.events = []

    def ci_info(self, stack):
        self.events.append(("info", stack.module_name, list(stack.ca_system_ids)))

    def ci_menu(self, stack, menu):
        self.events.append(("menu", menu))

    def ci_enquiry(self, stack, enq):
        self.events.append(("enq", enq))

    def ci_close(self, stack):
        self.events.append(("close",))


_clock = [1000.0]


def run(stack, module, steps=60):
    """Pump frames between host and module."""
    for _ in range(steps):
        _clock[0] += 0.2
        stack.poll(_clock[0])
        while module.to_host:
            stack.feed(module.to_host.pop(0))


def connected():
    module = FakeModule()
    stack = ci.CIStack(module.write)
    rec = Recorder()
    stack.listeners.append(rec)
    stack.start(_clock[0])
    for rid in (ci.RI_RESOURCE_MANAGER, ci.RI_APPLICATION_INFORMATION,
                ci.RI_CONDITIONAL_ACCESS_SUPPORT, ci.RI_DATE_TIME, ci.RI_MMI):
        module.open_session(rid)
    run(stack, module)
    return stack, module, rec


def test_length_roundtrip():
    for n in (0, 5, 0x7F, 0x80, 0xFF, 0x1234, 70000):
        enc = ci.encode_length(n)
        assert ci.decode_length(enc + b"xx", 0) == (n, len(enc))


def test_sessions_and_enquiries():
    stack, module, _rec = connected()
    assert set(stack.sessions.values()) == set(ci.HOST_RESOURCES)
    assert module.got(ci.AOT_PROFILE_ENQ)
    assert module.got(ci.AOT_APPLICATION_INFO_ENQ)
    assert module.got(ci.AOT_CA_INFO_ENQ)


def test_unknown_resource_refused():
    module = FakeModule()
    stack = ci.CIStack(module.write)
    stack.start(0.0)
    module.open_session(0x00990041)
    run(stack, module)
    assert stack.sessions == {}


def test_application_and_ca_info():
    stack, module, rec = connected()
    name = b"\x15" + "Modül CI".encode("utf-8")
    module.send_apdu(ci.RI_APPLICATION_INFORMATION, ci.AOT_APPLICATION_INFO,
                     bytes([1, 0x12, 0x34, 0x00, 0x01, len(name)]) + name)
    module.send_apdu(ci.RI_CONDITIONAL_ACCESS_SUPPORT, ci.AOT_CA_INFO, b"\x18\x01\x09\x8C")
    run(stack, module)
    assert stack.module_name == "Modül CI"
    assert stack.ca_system_ids == [0x1801, 0x098C]
    assert rec.events[-1][0] == "info"


def test_profile_exchange():
    stack, module, _rec = connected()
    module.send_apdu(ci.RI_RESOURCE_MANAGER, ci.AOT_PROFILE, b"")
    module.send_apdu(ci.RI_RESOURCE_MANAGER, ci.AOT_PROFILE_ENQ, b"")
    run(stack, module)
    assert module.got(ci.AOT_PROFILE_CHANGE)
    profile = module.got(ci.AOT_PROFILE)[0]
    ids = [int.from_bytes(profile[i:i + 4], "big") for i in range(0, len(profile), 4)]
    assert ids == list(ci.HOST_RESOURCES)


def test_menu_flow():
    stack, module, rec = connected()
    assert stack.enter_menu()
    run(stack, module)
    assert module.got(ci.AOT_ENTER_MENU) == [b""]

    module.send_apdu(ci.RI_MMI, ci.AOT_DISPLAY_CONTROL, bytes([ci.DCC_SET_MMI_MODE, 1]))
    body = bytes([2]) + text("Main menu") + text("Module") + text("Press OK") \
        + text("Smartcard information") + text("Parental control")
    module.send_apdu(ci.RI_MMI, ci.AOT_MENU_LAST, body)
    run(stack, module)
    assert module.got(ci.AOT_DISPLAY_REPLY) == [bytes([ci.DRI_MMI_MODE_ACK, ci.MM_HIGH_LEVEL])]
    kind, menu = rec.events[-1]
    assert kind == "menu" and not menu.is_list
    assert (menu.title, menu.subtitle, menu.bottom) == ("Main menu", "Module", "Press OK")
    assert menu.items == ["Smartcard information", "Parental control"]

    stack.answer_menu(2)
    run(stack, module)
    assert module.got(ci.AOT_MENU_ANSW) == [b"\x02"]

    module.send_apdu(ci.RI_MMI, ci.AOT_ENQ, bytes([1, 4]) + b"Enter PIN")
    run(stack, module)
    kind, enq = rec.events[-1]
    assert kind == "enq" and enq.blind and enq.length == 4 and enq.text == "Enter PIN"

    stack.answer_enquiry("1234")
    stack.answer_enquiry(None)
    run(stack, module)
    assert module.got(ci.AOT_ANSW) == [b"\x011234", b"\x00"]

    module.send_apdu(ci.RI_MMI, ci.AOT_CLOSE_MMI, b"\x00")
    run(stack, module)
    assert rec.events[-1] == ("close",)
    assert not stack.mmi_open


def test_list_is_flagged():
    stack, module, rec = connected()
    body = bytes([0xFF]) + text("Card") + text("") + text("") + text("Serial 123")
    module.send_apdu(ci.RI_MMI, ci.AOT_LIST_LAST, body)
    run(stack, module)
    kind, menu = rec.events[-1]
    assert kind == "menu" and menu.is_list and menu.items == ["Serial 123"]


def test_ca_pmt_waits_for_ca_info():
    stack, module, _rec = connected()
    stack.send_ca_pmt(b"\x03\x00\x01\x01\x00\x00")
    run(stack, module)
    assert module.got(ci.AOT_CA_PMT) == []            # no CA systems known yet
    module.send_apdu(ci.RI_CONDITIONAL_ACCESS_SUPPORT, ci.AOT_CA_INFO, b"\x18\x01")
    run(stack, module)
    assert module.got(ci.AOT_CA_PMT) == [b"\x03\x00\x01\x01\x00\x00"]


def test_long_spdu_is_chunked():
    stack, module, _rec = connected()
    module.send_apdu(ci.RI_CONDITIONAL_ACCESS_SUPPORT, ci.AOT_CA_INFO, b"\x18\x01")
    run(stack, module)
    big = bytes(range(256)) * 3
    stack.send_ca_pmt(big)
    run(stack, module, steps=120)
    assert module.got(ci.AOT_CA_PMT) == [big]


def test_date_time():
    stack, module, _rec = connected()
    module.send_apdu(ci.RI_DATE_TIME, ci.AOT_DATE_TIME_ENQ, b"\x00")
    run(stack, module)
    reply = module.got(ci.AOT_DATE_TIME)
    assert reply and len(reply[0]) == 5


def test_mjd():
    # 1 January 2000 00:00:00 UTC is MJD 51544
    assert ci.mjd_bcd_time(946684800) == struct.pack(">H", 51544) + b"\x00\x00\x00"


def test_close_session():
    stack, module, rec = connected()
    module.send_apdu(ci.RI_MMI, ci.AOT_DISPLAY_CONTROL, bytes([ci.DCC_SET_MMI_MODE, 1]))
    run(stack, module)
    nb = module.sessions[ci.RI_MMI]
    module.outbox.append(bytes([ci.ST_CLOSE_SESSION_REQUEST, 2]) + struct.pack(">H", nb))
    run(stack, module)
    assert ci.RI_MMI not in stack.sessions.values()
    assert rec.events[-1] == ("close",)


def test_dvb_text():
    assert ci.decode_dvb_text(b"Hello") == "Hello"
    assert ci.decode_dvb_text(b"\x05Gr\xfc\xdfe") == "Grüße"         # ISO 8859-9
    assert ci.decode_dvb_text(b"\x15" + "Grüße".encode()) == "Grüße"
    assert ci.decode_dvb_text(b"a\x8ab") == "a\nb"
