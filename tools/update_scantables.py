#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Pack a checkout or archive of dtv-scan-tables into lindvb/scantables.

    python3 tools/update_scantables.py path/to/dtv-scan-tables <commit> <date> [<source url>]
    python3 tools/update_scantables.py dtv-scan-tables-<commit>.tar.gz <commit> <date> [<source url>]

LinDVB takes the tables from Tvheadend's copy of dtv-scan-tables
(https://github.com/tvheadend/dtv-scan-tables, branch "tvheadend"): it holds
every commit of the LinuxTV repository (https://git.linuxtv.org/dtv-scan-tables.git)
and the newer lists people send to Tvheadend. The monthly workflow
.github/workflows/scantables.yml runs this script and opens a pull request
when the tables changed.

The tables (atsc, dvb-c, dvb-s, dvb-t, isdb-t; several thousand small files)
are read and written as one compressed index, ``dtv-scan-tables.json.gz``,
together with the project's licence files. lindvb.scantables reads it and
writes the initial tuning file for dvbv5-scan from it. The channels-conf
folder holds channel lists, not scan tables, and is left out.
"""

import gzip
import io
import json
import shutil
import sys
import tarfile
from pathlib import Path

SOURCE = "https://github.com/tvheadend/dtv-scan-tables"
FAMILIES = ("atsc", "dvb-c", "dvb-s", "dvb-t", "isdb-t")
OUT = Path(__file__).resolve().parent.parent / "lindvb" / "scantables"


def parse(text):
    """(title, [ {KEY: value} per [CHANNEL] block ]) of a dvbv5 scan table."""
    title, blocks, cur = "", [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if not title and not blocks and cur is None:
                title = line.lstrip("# ").strip()
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = {}
            blocks.append(cur)
        elif "=" in line and cur is not None:
            k, _, v = line.partition("=")
            cur[k.strip()] = v.strip()
    return title, [b for b in blocks if "FREQUENCY" in b and "DELIVERY_SYSTEM" in b]


def files(src):
    """(family, name, bytes) of every table in a folder or tar archive."""
    src = Path(src)
    if src.is_dir():
        for fam in FAMILIES:
            for f in sorted((src / fam).glob("*")):
                if f.is_file():
                    yield fam, f.name, f.read_bytes()
        for lic in ("COPYING", "COPYING.LGPL"):
            yield "", lic, (src / lic).read_bytes()
        return
    with tarfile.open(src) as tar:
        for m in sorted(tar.getmembers(), key=lambda m: m.name):
            parts = Path(m.name).parts
            if not m.isfile() or len(parts) < 1:
                continue
            if parts[-1] in ("COPYING", "COPYING.LGPL") and len(parts) <= 2:
                yield "", parts[-1], tar.extractfile(m).read()
            elif len(parts) >= 2 and parts[-2] in FAMILIES:
                yield parts[-2], parts[-1], tar.extractfile(m).read()


def main(src, commit, date, source=SOURCE):
    tables = []
    for fam, name, data in files(src):
        if not fam:
            (OUT / f"dtv-scan-tables-{name}").write_bytes(data)
            continue
        title, blocks = parse(data.decode("utf-8", errors="replace"))
        if blocks:
            tables.append({"family": fam, "name": name, "title": title, "channels": blocks})
    index = {"source": source,
             "commit": commit, "date": date, "tables": tables}
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=9) as gz:
        gz.write(json.dumps(index, ensure_ascii=False, separators=(",", ":"),
                            sort_keys=True).encode())
    (OUT / "dtv-scan-tables.json.gz").write_bytes(buf.getvalue())
    print(f"{len(tables)} tables, {len(buf.getvalue()) // 1024} KiB")


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        sys.exit(__doc__)
    main(*sys.argv[1:])
