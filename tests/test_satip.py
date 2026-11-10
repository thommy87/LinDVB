"""SAT>IP: tuning URLs, RTP/RTCP, tuner profiles, and a session and channel
search against a small SAT>IP server on localhost."""

import socket
import struct
import threading
import time

import pytest

from lindvb import satip, tunerprofiles as tp
from lindvb.channels import Channel
from lindvb.scscan import _Tp

from test_unicable import _packet, _section, _service


def sat(freq=11494000, pol="h", diseqc=-1, delsys="dvb-s2", **kw):
    return Channel("X", delsys=delsys, frequency=freq, polarity=pol, symbol_rate=22000,
                   diseqc=diseqc, sid=28, pmt_pid=0x100, **kw)


# -- tuning URLs ---------------------------------------------------------------------

def test_satellite_query():
    q = satip.tuning_query(sat(modulation="8psk", fec="2/3"), [0, 17, 0x100])
    assert q == ("src=1&freq=11494&pol=h&ro=0.35&msys=dvbs2&mtype=8psk&plts=off&sr=22000"
                 "&fec=23&pids=0,17,256")
    q = satip.tuning_query(sat(12188500, "v", diseqc=2, delsys="dvb-s", fec="3/4"))
    assert q.startswith("src=3&freq=12188.5&pol=v&ro=0.35&msys=dvbs&mtype=qpsk&sr=22000&fec=34")
    assert q.endswith("pids=all")
    # automatic modulation and FEC are left to the server
    assert "mtype" not in satip.tuning_query(sat()) and "fec" not in satip.tuning_query(sat())
    assert "isi=5" in satip.tuning_query(sat(stream_id=5))


def test_cable_and_terrestrial_query():
    cab = Channel("C", delsys="dvb-c-a", frequency=330000, symbol_rate=6900,
                  modulation="qam-256")
    assert satip.tuning_query(cab, "8192") == "freq=330&msys=dvbc&mtype=256qam&sr=6900&pids=all"
    ter = Channel("T", delsys="dvb-t2", frequency=538000, bandwidth_hz=8000000, stream_id=0)
    assert satip.tuning_query(ter, "0:16:17") == "freq=538&bw=8&msys=dvbt2&plp=0&pids=0,16,17"
    ter = Channel("T", delsys="dvb-t", frequency=474000, bandwidth_hz=7000000)
    assert satip.tuning_query(ter, []) == "freq=474&bw=7&msys=dvbt&pids=none"
    with pytest.raises(satip.SatipError):
        satip.tuning_query(Channel("A", delsys="atsc", frequency=1))


def test_pid_list():
    assert satip.pid_list("0:17:256") == "0,17,256"
    assert satip.pid_list([17, 0, 17]) == "0,17"
    assert satip.pid_list("8192") == "all"
    assert satip.pid_list("") == "none"


# -- RTP and RTCP ------------------------------------------------------------------

def rtp(payload, seq=1, csrc=0, ext=b"", pad=0):
    b0 = 0x80 | csrc | (0x10 if ext else 0) | (0x20 if pad else 0)
    head = struct.pack("!BBHII", b0, 33, seq, 0, 1234) + b"\0\0\0\1" * csrc
    if ext:
        head += struct.pack("!HH", 0xBEDE, len(ext) // 4) + ext
    return head + payload + (b"\0" * (pad - 1) + bytes([pad]) if pad else b"")


def rtcp_app(text):
    s = text.encode()
    body = b"SES1" + struct.pack("!HH", 0, len(s)) + s
    body += b"\0" * (-len(body) % 4)
    sr = struct.pack("!BBHI", 0x80, 200, 6, 1) + b"\0" * 20          # a sender report first
    return sr + struct.pack("!BBHI", 0x80, 204, (len(body) + 8) // 4 - 1, 1) + body


def test_rtp_payload():
    ts = b"\x47" + b"\0" * 187
    assert satip.rtp_payload(rtp(ts)) == ts
    assert satip.rtp_payload(rtp(ts, csrc=2, ext=b"abcd", pad=4)) == ts
    assert satip.rtp_payload(b"\x00" * 20) == b""


def test_rtcp_status():
    text = "ver=1.0;src=1;tuner=1,224,1,15,11494.00,h,dvbs2,8psk,off,0.35,22000,23;pids=0,17"
    assert satip.rtcp_app_strings(rtcp_app(text)) == [text]
    st = satip.parse_status(text)
    assert st["lock"] and st["signal"] == 224 * 65535 // 255 and st["snr"] == 65535
    assert satip.parse_status("ver=1.0;tuner=1,0,0,0") == {"signal": 0, "lock": False, "snr": 0}
    assert satip.parse_status("ver=1.0") == {}


# -- servers and tuner profiles ----------------------------------------------------

SERVER = {"id": 2, "host": "10.0.0.5", "port": 554, "name": "Octopus", "caps": "DVBS2-4,DVBT2-2",
          "transport": "tcp"}


def test_caps_and_profiles():
    assert satip.server_systems("DVBS2-4,DVBT2-2") == ["dvb-s", "dvb-s2", "dvb-t", "dvb-t2"]
    assert satip.tuner_count("DVBS2-4,DVBT2-2") == 4
    assert satip.tuner_count("") == 1
    profs = satip.server_profiles({"satip_servers": [SERVER]})
    assert [p["adapter"] for p in profs] == [1032, 1033, 1034, 1035]
    assert all(p["type"] == "satip" and p["host"] == "10.0.0.5" and p["transport"] == "tcp"
               for p in profs)
    assert profs[0]["name"] == "SAT>IP Octopus (10.0.0.5) #1"


def test_merge_keeps_choices_and_follows_servers():
    settings = {"satip_servers": [SERVER]}
    saved = [tp.new_profile(0), dict(satip.server_profiles(settings)[1], enabled=False,
                                     priority=5, host="old"),
             dict(satip.server_profiles(settings)[0], adapter=1999)]     # server gone
    out = satip.merge_profiles(saved, settings)
    by = {p["adapter"]: p for p in out}
    assert set(by) == {0, 1032, 1033, 1034, 1035}
    assert by[1033]["enabled"] is False and by[1033]["priority"] == 5
    assert by[1033]["host"] == "10.0.0.5"
    # saved settings or none: the network tuners are always there
    s = {"tuners": [tp.new_profile(0)], "satip_servers": [SERVER], "adapters": [0]}
    assert len(tp.configured_profiles(s)) == 5
    s = {"tuners": [], "satip_servers": [SERVER], "adapters": [0]}
    assert [p["adapter"] for p in tp.configured_profiles(s, detect=lambda: [])] == \
        [1032, 1033, 1034, 1035]


def test_choose_takes_a_satip_tuner():
    settings = {"satip_servers": [SERVER], "lnb_lof1": 9750000, "lnb_lof2": 10600000,
                "lnb_slof": 11700000}
    profs = tp.active_profiles(settings, detect=lambda: [])
    how = tp.choose(sat(), "live", profs, {}, settings)
    assert how[0] == "tune" and satip.is_satip(how[1])
    cable = Channel("C", delsys="dvb-c-a", frequency=330000, symbol_rate=6900)
    with pytest.raises(tp.NoTuner):
        tp.choose(cable, "live", profs, {}, settings)


def test_parse_description():
    xml = """<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0">
      <device><friendlyName>FRITZ!Box 6591 Cable</friendlyName><UDN>uuid:abc</UDN>
      <satip:X_SATIPCAP xmlns:satip="urn:ses-com:satip">DVBC-4</satip:X_SATIPCAP>
      </device></root>"""
    assert satip.parse_description(xml) == ("FRITZ!Box 6591 Cable", "uuid:abc", "DVBC-4")
    h = satip.parse_ssdp(b"HTTP/1.1 200 OK\r\nST: urn:ses-com:device:SatIPServer:1\r\n"
                         b"LOCATION: http://10.0.0.1:49000/satipdesc.xml\r\n\r\n")
    assert h["location"].endswith("satipdesc.xml")


# -- a SAT>IP server on localhost --------------------------------------------------

PAT = _section(0x00, 1101, bytes([0, 0, 0xE0, 0x10, 0, 28, 0xE1, 0x00]))
SDT = _section(0x42, 1101, _service(28, b"Das Erste", b"ARD"), extra=bytes([0, 1, 0xFF]))
PMT = _section(0x02, 28, bytes([0xE1, 0x01, 0xF0, 0x00, 0x1B, 0xE1, 0x01, 0xF0, 0x00,
                                0x03, 0xE1, 0x02, 0xF0, 0x00]))
STATUS = "ver=1.0;src=1;tuner=1,200,1,12,11494.00,h,dvbs2,8psk,off,0.35,22000,23;pids=0"


def _pcr_packet(pid, t):
    """An adaptation-field-only packet carrying PCR *t* (seconds)."""
    base = int(t * 90000) & ((1 << 33) - 1)
    pcr = bytes([base >> 25 & 0xFF, base >> 17 & 0xFF, base >> 9 & 0xFF, base >> 1 & 0xFF,
                 (base & 1) << 7 | 0x7E, 0])
    return bytes([0x47, pid >> 8, pid & 0xFF, 0x20, 183, 0x10]) + pcr + b"\xff" * 176


class FakeServer:
    """Answers SETUP/PLAY/OPTIONS/TEARDOWN and streams PAT, SDT and PMT."""

    def __init__(self, lock=True):
        self.lock = lock
        self.requests = []
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.stop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.stop.set()
        self.srv.close()

    def _accept(self):
        while not self.stop.is_set():
            try:
                conn, _addr = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        buf = b""
        state = {"udp": None, "tcp": False, "playing": threading.Event(), "end": False}
        send_lock = threading.Lock()
        while not self.stop.is_set():
            try:
                data = conn.recv(4096)
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\r\n\r\n" in buf:
                raw, buf = buf.split(b"\r\n\r\n", 1)
                lines = raw.decode().split("\r\n")
                method, url = lines[0].split()[:2]
                h = {k.lower(): v.strip() for k, _s, v in (x.partition(":") for x in lines[1:])}
                self.requests.append((method, url))
                extra = ""
                if method == "SETUP":
                    tr = h.get("transport", "")
                    if "TCP" in tr:
                        state["tcp"] = True
                    else:
                        port = int(tr.split("client_port=")[1].split("-")[0])
                        state["udp"] = port
                    extra = ("Session: 12345678;timeout=30\r\ncom.ses.streamID: 3\r\n"
                             f"Transport: {tr}\r\n")
                elif method == "PLAY":
                    if not state["playing"].is_set():
                        state["playing"].set()
                        threading.Thread(target=self._stream, args=(conn, state, send_lock),
                                         daemon=True).start()
                elif method == "TEARDOWN":
                    state["end"] = True
                try:
                    with send_lock:
                        conn.sendall(f"RTSP/1.0 200 OK\r\nCSeq: {h['cseq']}\r\n{extra}\r\n"
                                     .encode())
                except OSError:
                    break
        state["end"] = True
        conn.close()

    def _stream(self, conn, state, send_lock):
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        seq = 0
        ts = _packet(0, PAT) + _packet(0x11, SDT) + _packet(0x100, PMT)
        status = STATUS if self.lock else "ver=1.0;src=1;tuner=1,0,0,0;pids=0"
        while not state["end"] and not self.stop.is_set():
            seq += 1
            payload = ts + _pcr_packet(0x101, seq * 0.02)
            frames = [(0, rtp(payload if self.lock else b"", seq)), (1, rtcp_app(status))]
            for chan, frame in frames:
                if chan == 0 and not self.lock:
                    continue
                try:
                    if state["tcp"]:
                        with send_lock:
                            conn.sendall(b"$" + bytes([chan]) + struct.pack("!H", len(frame))
                                         + frame)
                    else:
                        udp.sendto(frame, ("127.0.0.1", state["udp"] + chan))
                except OSError:
                    state["end"] = True
            time.sleep(0.02)
        udp.close()


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.close()


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        time.sleep(0.02)
    return cond()


@pytest.mark.parametrize("transport", ["udp", "tcp"])
def test_session_streams_and_reports(server, transport):
    got, status = bytearray(), []
    sess = satip.Session("127.0.0.1", server.port, transport, on_data=got.extend,
                         on_status=status.append)
    sess.open(satip.tuning_query(sat(), [0, 17]))
    try:
        assert sess.session == "12345678" and sess.stream_id == "3"
        assert sess.keepalive == 15
        assert _wait(lambda: len(got) >= 3 * 188 and status)
        assert got[0] == 0x47 and len(got) % 188 == 0
        assert status[-1]["lock"] and status[-1]["signal"] == 200 * 65535 // 255
        sess.set_pids([0, 17, 256])
        assert _wait(lambda: any("pids=0,17,256" in u for _m, u in server.requests))
    finally:
        sess.close()
    assert _wait(lambda: ("TEARDOWN", f"rtsp://127.0.0.1:{server.port}/stream=3")
                 in server.requests)
    setup = server.requests[0]
    assert setup[0] == "SETUP" and "msys=dvbs2" in setup[1] and "pids=0,17" in setup[1]


def test_session_errors():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()                                   # nobody listens there
    with pytest.raises(satip.SatipError):
        satip.Session("127.0.0.1", port).open("freq=1")


@pytest.mark.parametrize("transport", ["udp", "tcp"])
def test_scan_finds_the_services(server, transport):
    prof = {"host": "127.0.0.1", "port": server.port, "transport": transport}
    lines = []
    chans = satip.scan(prof, [_Tp("dvb-s2", 11494000, "h", 22000)], diseqc=1, root="Astra",
                       log=lines.append)
    assert [(c.name, c.sid, c.tsid, c.onid, c.video_pid, c.diseqc) for c in chans] == \
        [("Das Erste", 28, 1101, 1, 0x101, 1)]
    assert "src=2" in server.requests[0][1]
    assert lines[-1] == "  1 services"


def test_scan_without_signal():
    s = FakeServer(lock=False)
    try:
        prof = {"host": "127.0.0.1", "port": s.port}
        lines, reports = [], []
        tps = [satip.ScanTp(Channel("C", delsys="dvb-c-a", frequency=330000, symbol_rate=6900))]
        assert satip.scan(prof, tps, log=lines.append, lock_wait=0.5,
                          on_status=reports.append) == []
        assert lines[-1] == "  no signal"
        assert reports and reports[-1] == {"signal": 0, "lock": False, "snr": 0}
    finally:
        s.close()


def test_entry_transponders():
    chans = [Channel("a", delsys="dvb-t2", frequency=538000, bandwidth_hz=8000000),
             Channel("b", delsys="dvb-t2", frequency=538000, bandwidth_hz=8000000),
             Channel("c", delsys="dvb-t", frequency=538000, bandwidth_hz=8000000)]
    tps = satip.entry_transponders(chans)
    assert [(t.delsys, t.frequency) for t in tps] == [("dvb-t2", 538000), ("dvb-t", 538000)]


def test_capture_records_from_a_satip_tuner(server, tmp_path):
    """The tuner's Capture: SETUP/PLAY on the server, the stream into the
    timeshift segments, the signal figures as "stats", TEARDOWN at the end."""
    Gst = pytest.importorskip("gi.repository.Gst")
    from gi.repository import GLib
    from lindvb import tuner
    Gst.init(None)
    settings = {"timeshift_dir": str(tmp_path), "timeshift_max_mb": 256,
                "full_transponder": False, "lnb_lof1": 9750000, "lnb_lof2": 10600000,
                "lnb_slof": 11700000, "hbbtv_enabled": False,
                "satip_servers": [{"id": 1, "host": "127.0.0.1", "port": server.port,
                                   "caps": "DVBS2-1", "transport": "tcp"}]}
    prof = satip.server_profiles(settings)[0]
    cap = tuner.Capture(sat(modulation="8psk"), prof["adapter"], 0, settings, tuner=prof)
    stats, errors = [], []
    cap.connect("stats", lambda _c, st: stats.append(st))
    cap.connect("error", lambda _c, e: errors.append(e))
    cap.start()
    loop = GLib.MainLoop()
    end = time.monotonic() + 4

    def check():
        if (stats and cap.store.bounds()[1] > 0) or time.monotonic() > end:
            loop.quit()
            return False
        return True
    GLib.timeout_add(50, check)
    loop.run()
    try:
        assert not errors
        assert stats and stats[-1]["lock"]
        assert cap.store.bounds()[1] > 0
        assert cap.source_label == "SAT>IP 127.0.0.1"
        cap.add_pid(0x200)                       # e.g. the AIT: PLAY with the new filter
        assert _wait(lambda: any("512" in u for m, u in server.requests if m == "PLAY"))
    finally:
        cap.destroy()
    assert _wait(lambda: any(m == "TEARDOWN" for m, _u in server.requests))
