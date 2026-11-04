#!/usr/bin/env python3
"""Write a small transport stream that signals one HbbTV application.

For testing HbbTV without DVB hardware: the stream has a PAT, a PMT with an
AIT entry (stream_type 0x05, application_signalling_descriptor) and an AIT
that starts URL as AUTOSTART application. Play it in LinDVB with

    LINDVB_TEST_TS=hbbtv.ts lindvb

on a channel whose service id is SID (default 28106, Das Erste HD on Astra).

    tools/hbbtv-test-ts.py http://localhost:8000/index.html -o hbbtv.ts

With --carousel DIR the application comes from a DSM-CC object carousel
instead (URL is then the entry page inside DIR, e.g. index.html), and
--event NAME=ID adds a stream event object events/stream with that event,
which --fire ID:TEXT then sends every few seconds with a counter:

    tools/hbbtv-test-ts.py index.html --carousel myapp --event quiz=1 --fire 1:Frage
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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


def es(stream_type, pid, info):
    return bytes([stream_type, 0xE0 | (pid >> 8), pid & 0xFF, 0xF0, len(info)]) + info


def build(url, sid, pmt_pid=0x100, ait_pid=0x101, name="Test", files=None, events=None,
          repeat=None, stream_events=(), fire=(), carousel_pid=0x102, event_pid=0x103):
    """files: {path: bytes} of an object carousel (component tag 0x0C) the
    application is loaded from; events: {path: ({name: id}, 0x0D)} stream
    event objects; stream_events: [(id, data, version)] sent all the time;
    fire: [(id, text)] sent with a new version every 100 rounds."""
    from lindvb import dsmcc
    pat = section(0x00, 1, sid.to_bytes(2, "big") + bytes([0xE0 | (pmt_pid >> 8), pmt_pid & 0xFF]))
    sig = desc(0x6F, bytes([0x80, 0x10, 0xE1]))
    streams = es(0x05, ait_pid, sig)
    if files is not None:
        streams += es(0x0B, carousel_pid, desc(0x52, b"\x0c") + desc(0x13, (7).to_bytes(4, "big")))
        streams += es(0x0C, event_pid, desc(0x52, b"\x0d"))
    pmt = section(0x02, sid, bytes([0xE1, 0xFF, 0xF0, 0x00]) + streams)
    app_d = desc(0x00, bytes([5, 0x00, 0x00, 1, 7, 1, 0xFF, 1, 1]))
    name_d = desc(0x01, b"deu" + bytes([len(name)]) + name.encode())
    if files is not None:
        loc = url.split("/", 3)[3] if url.startswith("dvb:") else url
        tp = desc(0x02, bytes([0x00, 0x01, 1, 0x00, 0x0C]))
    else:
        base, _, loc = url.rpartition("/")
        base += "/"
        tp = desc(0x02, bytes([0x00, 0x03, 1, len(base)]) + base.encode() + b"\x00")
    loc_d = desc(0x15, loc.encode())
    descs = app_d + name_d + tp + loc_d
    app = (0x13).to_bytes(4, "big") + (1).to_bytes(2, "big") + bytes([0x01]) \
        + bytes([0xF0 | (len(descs) >> 8), len(descs) & 0xFF]) + descs
    body = bytes([0xF0, 0x00, 0xF0 | (len(app) >> 8), len(app) & 0xFF]) + app
    ait = section(0x74, 0x0010, body)            # table id extension: application type
    carousel = dsmcc.CarouselWriter(files, events, compress=True).sections() if files is not None else []
    if repeat is None:
        repeat = 300 if files is not None else 2000
    out, cc = [], {0: 0, pmt_pid: 0, ait_pid: 0, carousel_pid: 0, event_pid: 0}
    for n in range(repeat):
        for pid, sec in ((0, pat), (pmt_pid, pmt), (ait_pid, ait)):
            data, cc[pid] = packets(pid, sec, cc[pid])
            out.append(data)
        if carousel:
            data, cc[carousel_pid] = dsmcc.ts_packets(carousel_pid, carousel, cc[carousel_pid])
            out.append(data)
        evs = [dsmcc.stream_event_section(eid, d, v) for eid, d, v in stream_events]
        evs += [dsmcc.stream_event_section(eid, f"{text} {n // 100 + 1}".encode(), (n // 100) % 32)
                for eid, text in fire]
        if evs:
            data, cc[event_pid] = dsmcc.ts_packets(event_pid, evs, cc[event_pid])
            out.append(data)
        out.append(b"\x47\x1f\xff\x10" + b"\xff" * 184)            # a null packet
    return b"".join(out)


def read_dir(path):
    files = {}
    for root, _dirs, names in os.walk(path):
        for n in names:
            full = os.path.join(root, n)
            with open(full, "rb") as f:
                files[os.path.relpath(full, path).replace(os.sep, "/")] = f.read()
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="entry page of the application")
    ap.add_argument("-o", "--output", default="hbbtv.ts")
    ap.add_argument("--sid", type=int, default=28106)
    ap.add_argument("--name", default="Test")
    ap.add_argument("--carousel", metavar="DIR", help="load the application from an object carousel")
    ap.add_argument("--event", action="append", default=[], metavar="NAME=ID",
                    help="stream event object events/stream with this event")
    ap.add_argument("--fire", action="append", default=[], metavar="ID:TEXT",
                    help="send this stream event every 100 rounds")
    a = ap.parse_args()
    files = read_dir(a.carousel) if a.carousel else None
    events = None
    if a.event:
        events = {"events/stream": ({n: int(i) for n, i in (e.split("=", 1) for e in a.event)}, 0x0D)}
    fire = [(int(i), t) for i, t in (f.split(":", 1) for f in a.fire)]
    with open(a.output, "wb") as f:
        f.write(build(a.url, a.sid, name=a.name, files=files, events=events, fire=fire))


if __name__ == "__main__":
    main()
