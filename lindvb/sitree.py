# SPDX-License-Identifier: GPL-2.0-or-later
"""PSI/SI sections as a tree, for the raw view of the channel analysis.

``decode(section)`` returns a Node: the table header, its loops and every
descriptor, decoded where LinDVB knows the descriptor (names, languages, CA,
service, teletext, subtitles, delivery systems, ...) and shown as hex otherwise.
``hexdump`` gives the classic offset / hex / ASCII listing.
"""

from .tsanalysis import (DESCRIPTOR_NAMES, SERVICE_TYPES, STREAM_TYPES,
                         ca_system_name, descriptors, dvb_text, mjd_utc, table_name)


class Node:
    __slots__ = ("label", "value", "children")

    def __init__(self, label, value="", children=None):
        self.label, self.value, self.children = label, value, children or []

    def add(self, label, value="", children=None):
        n = Node(label, value, children)
        self.children.append(n)
        return n

    def walk(self, depth=0):
        yield depth, self
        for c in self.children:
            yield from c.walk(depth + 1)


def _hex(b, limit=48):
    s = bytes(b[:limit]).hex(" ")
    return s + (" …" if len(b) > limit else "")


def _lang(b):
    return bytes(b[:3]).decode("latin-1", "replace")


def _bcd(b):
    return "".join(f"{x:02x}" for x in b)


def descriptor_node(tag, body):
    name = DESCRIPTOR_NAMES.get(tag, "user defined" if tag >= 0x80 else "reserved")
    n = Node(f"0x{tag:02X} {name}", f"{len(body)} bytes")
    try:
        if tag == 0x09 and len(body) >= 4:
            caid = (body[0] << 8) | body[1]
            n.add("CA_system_id", f"0x{caid:04X} {ca_system_name(caid)}".strip())
            n.add("CA_PID", f"0x{((body[2] & 0x1F) << 8) | body[3]:04X}")
        elif tag == 0x0A:
            for i in range(0, len(body) - 3, 4):
                n.add("language", f"{_lang(body[i:])}, audio_type {body[i + 3]}")
        elif tag in (0x40, 0x47):
            n.add("name", dvb_text(body))
        elif tag == 0x48 and len(body) >= 2:
            n.add("service_type", f"0x{body[0]:02X} {SERVICE_TYPES.get(body[0], '')}".strip())
            pl = body[1]
            n.add("provider", dvb_text(body[2:2 + pl]))
            nl = body[2 + pl]
            n.add("service_name", dvb_text(body[3 + pl:3 + pl + nl]))
        elif tag == 0x4D and len(body) >= 4:
            n.add("language", _lang(body))
            nl = body[3]
            n.add("event_name", dvb_text(body[4:4 + nl]))
            tl = body[4 + nl] if len(body) > 4 + nl else 0
            n.add("text", dvb_text(body[5 + nl:5 + nl + tl]))
        elif tag == 0x50 and len(body) >= 6:
            n.add("stream_content", f"0x{body[0] & 0x0F:X}")
            n.add("component_type", f"0x{body[1]:02X}")
            n.add("component_tag", str(body[2]))
            n.add("language", _lang(body[3:]))
            if len(body) > 6:
                n.add("text", dvb_text(body[6:]))
        elif tag == 0x52 and body:
            n.add("component_tag", str(body[0]))
        elif tag == 0x54:
            for i in range(0, len(body) - 1, 2):
                n.add("content", f"0x{body[i]:02X} / 0x{body[i + 1]:02X}")
        elif tag == 0x55:
            for i in range(0, len(body) - 3, 4):
                r = body[i + 3]
                n.add("rating", f"{_lang(body[i:])}: {r + 3 if 0 < r <= 0x0F else r}")
        elif tag == 0x56:
            for i in range(0, len(body) - 4, 5):
                mag = body[i + 3] & 7 or 8
                n.add("page", f"{_lang(body[i:])} type {body[i + 3] >> 3} "
                              f"page {mag}{body[i + 4]:02X}")
        elif tag == 0x59:
            for i in range(0, len(body) - 7, 8):
                n.add("subtitle", f"{_lang(body[i:])} type 0x{body[i + 3]:02X} "
                                  f"composition {(body[i + 4] << 8) | body[i + 5]} "
                                  f"ancillary {(body[i + 6] << 8) | body[i + 7]}")
        elif tag == 0x43 and len(body) >= 11:
            n.add("frequency", f"{_bcd(body[0:4])[:5]}.{_bcd(body[0:4])[5:]} GHz")
            pos = _bcd(body[4:6])
            n.add("orbital_position", f"{pos[:3].lstrip('0')}.{pos[3]}°{'E' if body[6] & 0x80 else 'W'}")
            n.add("polarisation", "HVLR"[(body[6] >> 5) & 3])
            n.add("modulation_system", "DVB-S2" if body[6] & 0x04 else "DVB-S")
            sr = _bcd(body[7:11])[:7]
            n.add("symbol_rate", f"{int(sr) / 10:.1f} kBd")
        elif tag == 0x44 and len(body) >= 11:
            n.add("frequency", f"{int(_bcd(body[0:4])) / 10000:.4f} MHz")
            n.add("modulation", f"0x{body[6]:02X}")
            n.add("symbol_rate", f"{int(_bcd(body[7:11])[:7]) / 10:.1f} kBd")
        elif tag == 0x5A and len(body) >= 4:
            f = int.from_bytes(body[0:4], "big") * 10
            n.add("centre_frequency", f"{f / 1e6:.3f} MHz")
        elif tag == 0x0E and len(body) >= 3:
            v = (((body[0] & 0x3F) << 16) | (body[1] << 8) | body[2]) * 400
            n.add("maximum_bitrate", f"{v / 1e6:.2f} Mbit/s")
        elif tag == 0x66 and len(body) >= 2:
            n.add("data_broadcast_id", f"0x{(body[0] << 8) | body[1]:04X}")
        elif tag == 0x6F:
            for i in range(0, len(body) - 2, 3):
                n.add("application_type", f"0x{((body[i] & 0x7F) << 8) | body[i + 1]:04X}, "
                                          f"AIT version {body[i + 2] & 0x1F}")
        elif tag == 0x83:
            for i in range(0, len(body) - 3, 4):
                n.add("service", f"0x{(body[i] << 8) | body[i + 1]:04X} → LCN "
                                 f"{((body[i + 2] & 0x03) << 8) | body[i + 3]}")
        elif tag == 0x5F and len(body) >= 4:
            n.add("private_data_specifier", f"0x{int.from_bytes(body[:4], 'big'):08X}")
        elif tag == 0x58:
            for i in range(0, len(body) - 12, 13):
                pol = "-" if body[i + 3] & 1 else "+"
                n.add("offset", f"{_lang(body[i:])}: {pol}{body[i + 4]:02x}:{body[i + 5]:02x}")
        else:
            n.add("data", _hex(body))
    except (IndexError, ValueError):
        n.add("data", _hex(body))
    return n


def _loop(parent, label, data):
    node = parent.add(label, f"{len(data)} bytes")
    for tag, body in descriptors(data):
        node.children.append(descriptor_node(tag, body))
    return node


def decode(sec):
    """Node tree of one section."""
    tid = sec[0]
    length = ((sec[1] & 0x0F) << 8) | sec[2]
    root = Node(table_name(tid), f"table_id 0x{tid:02X}, {length + 3} bytes")
    end = 3 + length
    try:
        if tid in (0x70, 0x73):
            import time
            utc = mjd_utc(sec[3:8])
            root.add("UTC_time", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(utc)))
            if tid == 0x73:
                dl = ((sec[8] & 0x0F) << 8) | sec[9]
                _loop(root, "descriptors", sec[10:10 + dl])
            return root
        ext = (sec[3] << 8) | sec[4]
        root.add("table_id_extension", f"{ext} (0x{ext:04X})")
        root.add("version_number", str((sec[5] >> 1) & 0x1F))
        root.add("current_next", str(sec[5] & 1))
        root.add("section", f"{sec[6]} / {sec[7]}")
        body_end = end - 4
        if tid == 0x00:
            for i in range(8, body_end - 3, 4):
                prog = (sec[i] << 8) | sec[i + 1]
                pid = ((sec[i + 2] & 0x1F) << 8) | sec[i + 3]
                root.add("network" if prog == 0 else f"program {prog}", f"PID 0x{pid:04X}")
        elif tid == 0x01:
            _loop(root, "descriptors", sec[8:body_end])
        elif tid == 0x02:
            root.add("PCR_PID", f"0x{((sec[8] & 0x1F) << 8) | sec[9]:04X}")
            pil = ((sec[10] & 0x0F) << 8) | sec[11]
            _loop(root, "program_info", sec[12:12 + pil])
            i = 12 + pil
            while i + 5 <= body_end:
                st = sec[i]
                pid = ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
                el = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
                kind = STREAM_TYPES.get(st, ("", f"0x{st:02X}"))[1]
                node = root.add(f"stream PID 0x{pid:04X}", f"stream_type 0x{st:02X} {kind}")
                for tag, b in descriptors(sec[i + 5:i + 5 + el]):
                    node.children.append(descriptor_node(tag, b))
                i += 5 + el
        elif tid in (0x42, 0x46):
            root.add("original_network_id", str((sec[8] << 8) | sec[9]))
            i = 11
            while i + 5 <= body_end:
                sid = (sec[i] << 8) | sec[i + 1]
                dl = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
                run = (sec[i + 3] >> 5) & 7
                node = root.add(f"service {sid}", f"running {run}, "
                                                   f"{'scrambled' if sec[i + 3] & 0x10 else 'free'}")
                for tag, b in descriptors(sec[i + 5:i + 5 + dl]):
                    node.children.append(descriptor_node(tag, b))
                i += 5 + dl
        elif tid in (0x40, 0x41, 0x4A):
            nl = ((sec[8] & 0x0F) << 8) | sec[9]
            _loop(root, "network descriptors", sec[10:10 + nl])
            i = 10 + nl
            tl = ((sec[i] & 0x0F) << 8) | sec[i + 1]
            i += 2
            stop = min(body_end, i + tl)
            while i + 6 <= stop:
                tsid = (sec[i] << 8) | sec[i + 1]
                onid = (sec[i + 2] << 8) | sec[i + 3]
                dl = ((sec[i + 4] & 0x0F) << 8) | sec[i + 5]
                node = root.add(f"transport stream {tsid}", f"ONID {onid}")
                for tag, b in descriptors(sec[i + 6:i + 6 + dl]):
                    node.children.append(descriptor_node(tag, b))
                i += 6 + dl
        elif 0x4E <= tid <= 0x6F:
            import time
            root.add("transport_stream_id", str((sec[8] << 8) | sec[9]))
            i = 14
            while i + 12 <= body_end:
                eid = (sec[i] << 8) | sec[i + 1]
                start = mjd_utc(sec[i + 2:i + 7])
                dur = _bcd(sec[i + 7:i + 10])
                dl = ((sec[i + 10] & 0x0F) << 8) | sec[i + 11]
                node = root.add(f"event {eid}", time.strftime("%d.%m. %H:%M", time.localtime(start))
                                + f", {dur[0:2]}:{dur[2:4]}:{dur[4:6]}")
                for tag, b in descriptors(sec[i + 12:i + 12 + dl]):
                    node.children.append(descriptor_node(tag, b))
                i += 12 + dl
        elif tid == 0x74:
            cl = ((sec[8] & 0x0F) << 8) | sec[9]
            _loop(root, "common descriptors", sec[10:10 + cl])
            i = 10 + cl
            al = ((sec[i] & 0x0F) << 8) | sec[i + 1]
            i += 2
            stop = min(body_end, i + al)
            while i + 9 <= stop:
                org = int.from_bytes(sec[i:i + 4], "big")
                app = (sec[i + 4] << 8) | sec[i + 5]
                dl = ((sec[i + 7] & 0x0F) << 8) | sec[i + 8]
                node = root.add(f"application 0x{org:X}/0x{app:X}", f"control code {sec[i + 6]}")
                for tag, b in descriptors(sec[i + 9:i + 9 + dl]):
                    node.children.append(descriptor_node(tag, b))
                i += 9 + dl
        root.add("CRC_32", f"0x{int.from_bytes(sec[end - 4:end], 'big'):08X}")
    except (IndexError, ValueError):
        root.add("(truncated)")
    return root


def hexdump(data, width=16):
    lines = []
    for off in range(0, len(data), width):
        chunk = bytes(data[off:off + width])
        hexs = " ".join(f"{b:02X}" for b in chunk).ljust(width * 3 - 1)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{off:04X}  {hexs}  {text}")
    return "\n".join(lines)
