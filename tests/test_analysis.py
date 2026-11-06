"""Channel analysis: stream parsers, link budget, loudness, logs and the TS analyser."""

import json
import math
import os
import struct

import pytest

from lindvb import esparse, festats, linkbudget, loudness, sitree, streaminfo
from lindvb.analysis import ca_lines, service_layers, signal_sample, transponder_shares
from lindvb.channels import Channel
from lindvb.receptionlog import ReceptionLog
from lindvb.tsanalysis import NULL_PID, TsAnalyzer, pid_owners


# ---------------------------------------------------------------- a small TS writer

def crc32_mpeg(data):
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def section(tid, ext, body, version=0):
    length = 5 + len(body) + 4
    head = bytes([tid, 0xB0 | (length >> 8), length & 0xFF, ext >> 8, ext & 0xFF,
                  0xC1 | (version << 1), 0, 0])
    sec = head + body
    return sec + struct.pack(">I", crc32_mpeg(sec))


class Muxer:
    def __init__(self):
        self.cc = {}

    def packets(self, pid, payload, pusi=True, pcr=None, scrambled=0):
        out = []
        first = True
        while payload or first:
            cc = self.cc.get(pid, 0)
            self.cc[pid] = (cc + 1) & 15
            af = b""
            if pcr is not None and first:
                base = pcr
                af = bytes([7, 0x10]) + struct.pack(">IH", base >> 1, ((base & 1) << 15) | 0x7E00)
            room = 184 - len(af)
            chunk, payload = payload[:room], payload[room:]
            if len(chunk) < room:
                pad = room - len(chunk)
                if af:
                    af = bytes([af[0] + pad]) + af[1:] + b"\xff" * pad
                elif pad == 1:
                    af = b"\x00"
                else:
                    af = bytes([pad - 1, 0]) + b"\xff" * (pad - 2)
            afc = 0x30 if af else 0x10
            head = bytes([0x47, (0x40 if (pusi and first) else 0) | (pid >> 8), pid & 0xFF,
                          (scrambled << 6) | afc | cc])
            pkt = head + af + chunk
            assert len(pkt) == 188, len(pkt)
            out.append(pkt)
            first = False
        return b"".join(out)

    def psi(self, pid, sec):
        return self.packets(pid, b"\x00" + sec)


def pes(stream_id, pts, data):
    p = (0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF, 0x01 | ((pts >> 14) & 0xFE),
         (pts >> 7) & 0xFF, 0x01 | ((pts << 1) & 0xFE))
    head = bytes([0x80, 0x80, 5]) + bytes(p)
    length = len(head) + len(data)
    return b"\x00\x00\x01" + bytes([stream_id]) + struct.pack(">H", length) + head + data


MPA_FRAME = bytes.fromhex("fffd9404") + bytes(476)        # 160 kbit/s, 48 kHz, stereo


def build_ts(seconds=2.0, cc_error=False):
    m = Muxer()
    pat = section(0x00, 0x0001, struct.pack(">HH", 0x0101, 0xE000 | 0x100))
    lang = bytes([0x0A, 4]) + b"deu" + b"\x00"
    pmt_body = struct.pack(">HH", 0xE000 | 0x101, 0xF000) + \
        bytes([0x03]) + struct.pack(">HH", 0xE000 | 0x101, 0xF000 | len(lang)) + lang
    pmt = section(0x02, 0x0101, pmt_body)
    sdt_body = struct.pack(">HB", 1, 0xFF)
    name, prov = b"Testkanal", b"LinDVB"
    desc = bytes([0x48, 3 + len(prov) + len(name), 0x01, len(prov)]) + prov + bytes([len(name)]) + name
    sdt_body += struct.pack(">HBH", 0x0101, 0xFC, 0x8000 | len(desc)) + desc
    sdt = section(0x42, 0x0001, sdt_body)
    out = []
    frames = int(seconds / 0.024)
    for i in range(frames):
        if i % 4 == 0:
            out.append(m.psi(0, pat))
            out.append(m.psi(0x100, pmt))
            out.append(m.psi(0x11, sdt))
        pts = 90000 + i * 2160
        out.append(m.packets(0x101, pes(0xC0, pts, MPA_FRAME), pcr=pts * 300 // 300 * 300 // 300))
        if cc_error and i == frames // 2:
            m.cc[0x101] = (m.cc[0x101] + 5) & 15
        out.append(m.packets(NULL_PID, b"\xff" * 184, pusi=False))
    return b"".join(out), frames


# ---------------------------------------------------------------- elementary streams

def test_h264_sps():
    sps = bytes.fromhex("67640028acd940780227e5c04400000300040000030" "0c83c60c658")
    info = esparse.parse_h264_sps(sps)
    assert (info["width"], info["height"]) == (1920, 1080)
    assert info["profile"] == "High" and info["level"] == "4.0"
    assert info["chroma"] == "4:2:0" and not info["interlaced"]
    assert info["fps"] == 25.0


def test_audio_headers():
    ac3 = esparse.parse_audio(bytes.fromhex("0b7700001c40e100") + bytes(20))
    assert ac3["short"] == "AC-3" and ac3["nominal_bps"] == 384000
    assert ac3["layout"] == "5.1" and ac3["dialnorm"] == -31
    mpa = esparse.parse_audio(MPA_FRAME)
    assert mpa["short"] == "MP2" and mpa["nominal_bps"] == 160000 and mpa["rate"] == 48000
    aac = esparse.parse_audio(bytes.fromhex("fff150802e7ffc") + bytes(10))
    assert aac["short"] == "AAC" and aac["rate"] == 44100 and aac["channels"] == 2
    assert esparse.parse_audio(b"\x00" * 16) is None or not esparse.parse_audio(b"\x00" * 16)


def test_pes_header_reads_pts():
    sid, pts, off = esparse.pes_header(pes(0xC0, 123456, b"abc"))
    assert sid == 0xC0 and pts == 123456


# ---------------------------------------------------------------- link budget

def test_link_budget():
    assert linkbudget.normalise_fec("FEC_2_3") == "2/3"
    assert linkbudget.normalise_modulation("QAM_256") == "qam-256"
    need = linkbudget.required_cn("dvb-s2", "8psk", "2/3")
    assert 6 < need < 7.5
    assert linkbudget.margin(10, "dvb-s2", "8psk", "2/3") == pytest.approx(10 - need)
    cap = linkbudget.capacity_bps("dvb-s2", "8psk", "2/3", 22000000, pilot="on")
    assert 41e6 < cap < 44e6
    cap_t = linkbudget.capacity_bps("dvb-t", "qam64", "2/3", bandwidth_hz=8000000, guard="1/4")
    assert cap_t == pytest.approx(19.91e6, rel=0.01)
    assert linkbudget.required_cn("dvb-s2", "auto", "auto") is None


def test_signal_sample_margin():
    ch = Channel(name="X", delsys="dvb-s2", frequency=11494000, polarity="h", symbol_rate=22000,
                 modulation="8psk", fec="2/3")
    s = signal_sample({"lock": True, "signal": 50000, "snr": 40000, "unc": 3},
                      {"cnr_db": 11.0, "signal_dbm": -42.0}, {}, ch)
    assert s["lock"] and s["cnr_db"] == 11.0 and s["level_dbm"] == -42.0
    assert s["margin"] == pytest.approx(11.0 - linkbudget.required_cn("dvb-s2", "8psk", "2/3"))
    assert 0 < s["quality"] <= 1


# ---------------------------------------------------------------- loudness

def test_loudness_of_a_steady_tone():
    m = loudness.LoudnessMeter()
    for _ in range(100):              # 10 s of 0.1 s blocks
        m.feed([0.125, 0.125])
    assert m.momentary == pytest.approx(-6.71, abs=0.01)
    assert m.integrated == pytest.approx(-6.71, abs=0.01)
    assert m.loudness_range == pytest.approx(0.0, abs=0.01)


def test_k_weighting_is_unity_at_mid_frequencies():
    b, a = loudness.k_weighting(48000)
    w = 2 * math.pi * 1000 / 48000
    z = complex(math.cos(w), math.sin(w))
    gain = abs(sum(c * z ** -i for i, c in enumerate(b)) / sum(c * z ** -i for i, c in enumerate(a)))
    assert 20 * math.log10(gain) == pytest.approx(0.7, abs=0.4)


def test_loudness_jump_is_noticed():
    m = loudness.LoudnessMeter()
    for _ in range(1050):              # the reference needs 100 s of programme
        m.feed([0.002, 0.002])
    assert not m.jumps
    for _ in range(60):
        m.feed([0.1, 0.1])
    assert m.jumps and m.jumps[-1][1] > 6


# ---------------------------------------------------------------- internet streams

def test_hls_master_and_media():
    master = streaminfo.parse_hls(
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1280000,RESOLUTION=1280x720,"
        "CODECS=\"avc1.4d401f,mp4a.40.2\",FRAME-RATE=50\nhd.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=640000,RESOLUTION=640x360\nsd.m3u8\n",
        "https://x.org/live/m.m3u8")
    assert master["kind"] == "master"
    assert [v["height"] for v in master["variants"]] == [360, 720]
    assert master["variants"][1]["uri"] == "https://x.org/live/hd.m3u8"
    media = streaminfo.parse_hls("#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:3.84,\na.ts\n"
                                 "#EXTINF:4.0,\nb.ts\n")
    assert media["kind"] == "media" and media["segments"] == 2 and media["live"]
    assert media["avg_segment"] == pytest.approx(3.92)


def test_dash_manifest():
    mpd = streaminfo.parse_mpd(
        '<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic">'
        '<Period><AdaptationSet contentType="video"><Representation id="v" bandwidth="3000000"'
        ' width="1280" height="720" codecs="avc1.64001f"/></AdaptationSet>'
        '<AdaptationSet contentType="audio" lang="de"><Representation id="a" bandwidth="128000"/>'
        '</AdaptationSet></Period></MPD>')
    assert mpd["kind"] == "dash" and mpd["live"]
    assert {r["type"] for r in mpd["representations"]} == {"video", "audio"}


# ---------------------------------------------------------------- frontend statistics

def test_frontend_stats_decoding():
    st = festats.decode_stats({festats.DTV_STAT_CNR: (festats.FE_SCALE_DECIBEL, 12340),
                               festats.DTV_STAT_SIGNAL_STRENGTH: (festats.FE_SCALE_RELATIVE, 32768),
                               festats.DTV_STAT_ERROR_BLOCK_COUNT: (festats.FE_SCALE_COUNTER, 7),
                               festats.DTV_STAT_TOTAL_BLOCK_COUNT: (festats.FE_SCALE_COUNTER, 1000)})
    assert st["cnr_db"] == pytest.approx(12.34)
    assert st["signal_rel"] == pytest.approx(0.5, abs=0.01)
    assert st["block_errors"] == 7 and st["block_total"] == 1000
    assert festats.Frontend(99, root="/nonexistent").available is False


def test_property_struct_layout():
    import ctypes
    assert ctypes.sizeof(festats.DtvProperty) == 76
    assert ctypes.sizeof(festats.DtvProperties) == 16


# ---------------------------------------------------------------- reception log

def test_reception_log_writes_minutes(tmp_path):
    t = {"now": 1_700_000_000.0}
    log = ReceptionLog(tmp_path, clock=lambda: t["now"])
    ch = Channel(name="Das Erste", delsys="dvb-s2", frequency=11494000, polarity="h", sid=1)
    for i in range(120):
        log.feed(ch, 0.8, cnr_db=10.0 + (i % 2), unc=i // 60, lock=True)
        t["now"] += 1
    log.flush()
    rows = log.read(1, "Das Erste")
    assert len(rows) >= 2
    assert rows[0]["quality_avg"] == pytest.approx(0.8)
    assert 10.0 <= rows[0]["cnr_avg"] <= 11.0
    assert log.read(1, "Other") == []


# ---------------------------------------------------------------- the TS analyser

def test_ts_analyser_finds_the_service():
    data, frames = build_ts(2.0, cc_error=True)
    a = TsAnalyzer(sid=0x0101)
    for i in range(0, len(data), 188 * 50):
        a.feed(data[i:i + 188 * 50], i / len(data) * 2.0)
    snap = a.snapshot()
    assert snap["packets"] == len(data) // 188 and snap["sync_losses"] == 0
    assert snap["pat"] == {0x0101: 0x100}
    pmt = snap["pmt"]
    assert pmt["sid"] == 0x0101 and pmt["pcr_pid"] == 0x101
    streams = dict(pmt["streams"])
    assert streams[0x101]["kind"] == "audio" and streams[0x101]["language"] == "deu"
    es = snap["es"][0x101]
    assert es["short"] == "MP2" and es["nominal_bps"] == 160000
    assert snap["cc_errors"] == 1 and snap["pids"][0x101]["cc_errors"] == 1
    assert snap["services"][0x0101]["name"] == "Testkanal"
    assert snap["services"][0x0101]["provider"] == "LinDVB"
    assert {t["name"] for t in snap["tables"]} >= {"PAT", "PMT", "SDT actual"}
    assert "PMT" in snap["raw"]
    owners = pid_owners(snap)
    assert owners[0x100] == (0x0101, "PMT")
    assert any(kind == "cc" for _t, kind, _d in snap["events"])


def test_raw_view_tree_and_hex():
    pat = section(0x00, 0x0001, struct.pack(">HH", 0x0101, 0xE100))
    root = sitree.decode(pat)
    text = "\n".join(f"{n.label} {n.value}" for _d, n in root.walk())
    assert "PAT" in text and "0x0100" in text
    dump = sitree.hexdump(pat)
    assert dump.startswith("0000  00 B0") or dump.startswith("0000  00 b0")


def test_layers_shares_and_ca():
    pmt = {"sid": 1, "pcr_pid": 0x101, "ca": [(0x1702, 0x150)],
           "streams": [(0x101, {"kind": "video", "codec": "H.264", "ca": []}),
                       (0x102, {"kind": "audio", "codec": "AC-3", "ca": []}),
                       (0x103, {"kind": "teletext", "codec": "Teletext", "ca": []})]}
    rates = {0x101: 4e6, 0x102: 192e3, 0x103: 50e3, 0x100: 10e3, 0x150: 5e3, NULL_PID: 1e6}
    layers = service_layers(pmt, rates, 0x100)
    assert layers[0] == (0x101, 4e6, "video")
    assert layers[-1] == ("other", pytest.approx(65e3), "other")
    snap = {"pat": {1: 0x100, 2: 0x200}, "pmts": {1: pmt}, "pmt": pmt,
            "services": {1: {"name": "A", "scrambled": True}, 2: {"name": "B"}}}
    shares = transponder_shares(snap, rates)
    assert shares["services"][0]["name"] == "A" and shares["null"] == 1e6
    assert ca_lines(snap)[0][0] == 0x1702


def test_black_borders():
    pytest.importorskip("gi")
    from lindvb.player import black_borders
    w, h = 64, 48
    rows = []
    for y in range(h):
        bright = 6 <= y < h - 6
        rows.append(bytes([200 if bright else 10] * w))
    t, b, lft, r = black_borders(b"".join(rows), w, h, w)
    assert t == pytest.approx(6 / 48) and b == pytest.approx(6 / 48)
    assert lft == 0 and r == 0
    assert black_borders(bytes(w * h), w, h, w) is None


def test_report_json_is_plain(tmp_path):
    from lindvb.analysis import _plain
    out = _plain({"a": (1, 2), "b": float("inf"), "c": b"x", 3: [1.5]})
    assert json.loads(json.dumps(out)) == {"a": [1, 2], "b": None, "3": [1.5]}


@pytest.mark.skipif(not os.path.exists("/dev/null"), reason="no /dev")
def test_free_tuners_needs_a_device(monkeypatch):
    from lindvb.tuner import free_tuners

    class M:
        settings = {"adapters": [7]}
        captures = {}
    monkeypatch.delenv("LINDVB_TEST_TS", raising=False)
    assert free_tuners(M) == ([7] if os.path.exists("/dev/dvb/adapter7") else [])
    monkeypatch.setenv("LINDVB_TEST_TS", "/tmp/x.ts")
    assert free_tuners(M) == [7]
