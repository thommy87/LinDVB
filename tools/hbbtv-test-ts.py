#!/usr/bin/env python3
"""Write a small transport stream that signals one HbbTV application.

For testing HbbTV without DVB hardware: the stream has a PAT, a PMT with an
AIT entry (stream_type 0x05, application_signalling_descriptor) and an AIT
that starts URL as AUTOSTART application. Play it in LinDVB with

    LINDVB_TEST_TS=hbbtv.ts lindvb

on a channel whose service id is SID (default 28106, Das Erste HD on Astra).

    tools/hbbtv-test-ts.py http://localhost:8000/index.html -o hbbtv.ts
"""

import argparse


def crc32_mpeg(data):
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else (crc << 1)
            crc &= 0xFFFFFFFF
    return crc


def section(table_id, ext, body, version=1):
    head = bytes([table_id, 0, 0]) + ext.to_bytes(2, "big") + bytes([0xC1 | (version << 1), 0, 0])
    sec = bytearray(head + body)
    length = len(sec) - 3 + 4
    sec[1], sec[2] = 0xB0 | (length >> 8), length & 0xFF
    return bytes(sec) + crc32_mpeg(sec).to_bytes(4, "big")


def desc(tag, body):
    return bytes([tag, len(body)]) + body


def packets(pid, sec, cc):
    data, out, first = b"\x00" + sec, [], True
    while data:
        chunk, data = data[:184], data[184:]
        out.append(bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF, 0x10 | (cc % 16)])
                   + chunk + b"\xff" * (184 - len(chunk)))
        cc, first = cc + 1, False
    return b"".join(out), cc


def build(url, sid, pmt_pid=0x100, ait_pid=0x101, name="Test"):
    base, _, loc = url.rpartition("/")
    base += "/"
    pat = section(0x00, 1, sid.to_bytes(2, "big") + bytes([0xE0 | (pmt_pid >> 8), pmt_pid & 0xFF]))
    sig = desc(0x6F, bytes([0x80, 0x10, 0xE1]))
    pmt = section(0x02, sid, bytes([0xE1, 0xFF, 0xF0, 0x00])
                  + bytes([0x05, 0xE0 | (ait_pid >> 8), ait_pid & 0xFF, 0xF0, len(sig)]) + sig)
    app_d = desc(0x00, bytes([5, 0x00, 0x00, 1, 7, 1, 0xFF, 1, 1]))
    name_d = desc(0x01, b"deu" + bytes([len(name)]) + name.encode())
    tp = desc(0x02, bytes([0x00, 0x03, 1, len(base)]) + base.encode() + b"\x00")
    loc_d = desc(0x15, loc.encode())
    descs = app_d + name_d + tp + loc_d
    app = (0x13).to_bytes(4, "big") + (1).to_bytes(2, "big") + bytes([0x01]) \
        + bytes([0xF0 | (len(descs) >> 8), len(descs) & 0xFF]) + descs
    body = bytes([0xF0, 0x00, 0xF0 | (len(app) >> 8), len(app) & 0xFF]) + app
    ait = section(0x74, 0x0010, body)            # table id extension: application type
    out, cc = [], {0: 0, pmt_pid: 0, ait_pid: 0}
    for _ in range(2000):
        for pid, sec in ((0, pat), (pmt_pid, pmt), (ait_pid, ait)):
            data, cc[pid] = packets(pid, sec, cc[pid])
            out.append(data)
        out.append(b"\x47\x1f\xff\x10" + b"\xff" * 184)            # a null packet
    return b"".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="entry page of the application")
    ap.add_argument("-o", "--output", default="hbbtv.ts")
    ap.add_argument("--sid", type=int, default=28106)
    ap.add_argument("--name", default="Test")
    a = ap.parse_args()
    with open(a.output, "wb") as f:
        f.write(build(a.url, a.sid, name=a.name))


if __name__ == "__main__":
    main()
