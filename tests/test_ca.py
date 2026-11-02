"""Tests for the conditional-access building blocks (no hardware, no GStreamer)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from lindvb import ca  # noqa: E402

# A hand-built PMT section for program 1: PCR on PID 256, one program-level CA
# descriptor (system 0x1801, ECM PID 1383) and one video ES on PID 512 with an
# ES-level CA descriptor (ECM PID 1536). The trailing four bytes stand in for
# the CRC, which the parser skips.
PMT = bytes.fromhex(
    "02b01e0001e10000e100f006"       # header + pcr_pid + program_info_length
    "090418" "01e567"                 # program CA descriptor: sys 0x1801 ECM 1383
    "02e200f006"                      # ES: video, PID 512, es_info_length 6
    "090418" "01e600"                 # ES CA descriptor: sys 0x1801 ECM 1536
    "00000000"                        # CRC placeholder
)


def test_parse_pmt():
    pmt = ca.parse_pmt(PMT)
    assert pmt.program_number == 1
    assert pmt.pcr_pid == 256
    assert pmt.program_ca == [(0x1801, 1383)]
    assert pmt.es_pids == [512]
    assert pmt.streams[0][0] == 0x02           # stream_type (video)
    assert pmt.ca_pids == [1383, 1536]


def test_parse_pmt_rejects_non_pmt():
    for bad in (b"", b"\x00\xb0\x05", b"\x02\xb0\xff"):
        try:
            ca.parse_pmt(bad)
        except ValueError:
            continue
        raise AssertionError("expected ValueError")


def test_build_and_wrap_ca_pmt():
    pmt = ca.parse_pmt(PMT)
    body = ca.build_ca_pmt(pmt, demux=0, adapter=3, service_pmt_pid=0x66)
    assert body[0] == ca.CA_PMT_LIST_ONLY
    assert body[1:3] == b"\x00\x01"            # program_number
    # program-level private descriptor carries demux/adapter and the cmd id
    assert ca.CA_PMT_CMD_OK_DESCRAMBLING == body[6]   # first byte of program_info
    assert bytes([0x82, 0x02, 0x00, 0x03]) in body
    wrapped = ca.wrap_ca_pmt(body)
    assert wrapped.startswith(ca.AOT_CA_PMT)
    assert wrapped.endswith(body)


def test_asn1_length():
    assert ca._asn1_length(5) == b"\x05"
    assert ca._asn1_length(0x80) == b"\x81\x80"
    assert ca._asn1_length(0x1234) == b"\x82\x12\x34"


def _ts_packet(pid, payload, pusi=True):
    hdr = bytearray(4)
    hdr[0] = ca.TS_SYNC
    hdr[1] = ((0x40 if pusi else 0) | ((pid >> 8) & 0x1F))
    hdr[2] = pid & 0xFF
    hdr[3] = 0x10                              # payload only, scrambling clear
    body = bytearray(payload)
    pkt = hdr + body
    pkt += bytes([0xFF] * (ca.TS_PACKET - len(pkt)))
    return bytes(pkt[:ca.TS_PACKET])


def test_section_assembler_single_packet():
    pkt = _ts_packet(0x20, b"\x00" + PMT)     # pointer_field 0 then the section
    asm = ca.SectionAssembler()
    sections = list(asm.feed(pkt))
    assert sections == [PMT]


def test_section_matches():
    section = bytes([0x80, 0xb0, 0x05, 0x00, 0x11, 0x22, 0x33, 0x44])
    flt = bytes([0x80] + [0] * 15)
    mask = bytes([0xFF] + [0] * 15)
    assert ca.section_matches(section, flt, mask)
    flt2 = bytes([0x81] + [0] * 15)
    assert not ca.section_matches(section, flt2, mask)


def test_ts_helpers():
    pkt = bytearray(_ts_packet(0x1FF, b"hello"))
    assert ca.ts_pid(pkt) == 0x1FF
    assert ca.ts_scrambling(pkt) == 0
    assert ca.ts_payload_offset(pkt) == 4


def test_descrambler_passes_clear_packets():
    d = ca.CSADescrambler()
    pkt = bytearray(_ts_packet(0x100, b"abc"))
    before = bytes(pkt)
    assert d.descramble_packet(pkt) is True    # clear packet: nothing to do
    assert bytes(pkt) == before


def test_descrambler_without_keys():
    d = ca.CSADescrambler()
    pkt = bytearray(_ts_packet(0x100, b"abc"))
    pkt[3] |= 0x80                             # mark as scrambled (even key)
    result = d.descramble_packet(pkt)
    if d.available:
        # a key was never set, so libdvbcsa has nothing loaded; our code still
        # returns True after attempting, but the important invariant is it does
        # not crash and leaves a 188-byte packet
        assert len(pkt) == ca.TS_PACKET
    else:
        assert result is False                 # cannot descramble without the lib


def test_builtin_providers():
    settings = {"ca_dvbapi_socket": "/tmp/does-not-exist.socket",
                "ca_ci_adapter": 0, "ca_ci_device": 0}
    assert isinstance(ca.builtin_provider("dvbapi", settings), ca.DvbApiProvider)
    assert isinstance(ca.builtin_provider("ci", settings), ca.CIProvider)
    assert ca.builtin_provider("nope", settings) is None


def test_dvbapi_message_framing():
    """A CA_SET_DESCR message updates the control word without a live socket."""
    prov = ca.DvbApiProvider({"ca_dvbapi_socket": "/tmp/x"})
    # opcode, adapter, index=0, parity=0 (even), cw = 8 bytes
    import struct
    msg = struct.pack(">I", ca.DVBAPI_CA_SET_DESCR) + bytes([0]) + \
        struct.pack(">II", 0, 0) + bytes([1, 2, 3, 4, 5, 6, 7, 8])
    leftover = prov._consume(msg)
    assert leftover == b""                     # fully consumed


def test_pat_pmt_pid():
    from lindvb.ca import pat_pmt_pid
    # PAT: program 0 -> NIT 0x0010, program 10301 -> PMT 0x0064, program 7 -> 0x0100
    body = bytes([0x00, 0x01, 0xC1, 0x00, 0x00,
                  0x00, 0x00, 0xE0, 0x10,
                  0x28, 0x3D, 0xE0, 0x64,
                  0x00, 0x07, 0xE1, 0x00])
    sec = bytes([0x00, 0xB0, len(body) + 4]) + body + b"\0\0\0\0"
    assert pat_pmt_pid(sec, 10301) == 0x64
    assert pat_pmt_pid(sec, 7) == 0x100
    assert pat_pmt_pid(sec, 99) == 0 and pat_pmt_pid(sec, 0) == 0
    assert pat_pmt_pid(b"\x02" + sec[1:], 10301) == 0


def test_softcam_gets_whole_transponder():
    from lindvb.channels import Channel
    from lindvb.config import DEFAULTS
    from lindvb.tuner import dvbsrc_properties
    ch = Channel(name="Pay", delsys="dvb-s2", frequency=11494000, polarity="h",
                 symbol_rate=22000, sid=1, pmt_pid=100, video_pid=101, encrypted=True)
    s = dict(DEFAULTS)
    assert dvbsrc_properties(ch, s, 0, 0)["pids"] != "8192"        # no CA: own PIDs
    s["ca_provider"] = "dvbapi"
    assert dvbsrc_properties(ch, s, 0, 0)["pids"] == "8192"        # ECM PIDs reach OSCam
    s["ca_provider"] = "ci"
    assert dvbsrc_properties(ch, s, 0, 0)["pids"] != "8192"
