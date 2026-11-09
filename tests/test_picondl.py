from lindvb import picondl, picons
from lindvb.channels import Channel

SRP = b"283D_3FB_1_C00000=daserstehd\n2B66_3F3_1_FFFF0000=kabeleins\nbroken line\n"
SNP = b"zdfhd=zdfhd\nbayern3=bayern3\n"


def fake_server(calls):
    files = {"srp.index": SRP, "snp.index": SNP,
             "logos/daserstehd.default.svg": b"<svg/>",
             "logos/kabeleins.default.png": b"PNG",
             "logos/zdfhd.default.png": b"PNG"}

    def fetch(url):
        calls.append(url)
        return files.get(url[len(picondl.BASE):])
    return fetch


def test_download_only_the_channels_logos(tmp_path):
    (tmp_path / "bayern3.png").write_bytes(b"own")          # already there
    ard = Channel("Das Erste HD", delsys="dvb-s2", root="Astra 19.2°E", sid=0x283D,
                  tsid=0x3FB, onid=1)
    ard2 = Channel("Das Erste HD", delsys="dvb-s2", root="Astra 19.2°E", sid=0x283D,
                   tsid=0x3FB, onid=1)
    cab = Channel("Kabel Eins", delsys="dvb-c-a", sid=0x2B66, tsid=0x3F3, onid=1)
    zdf = Channel("ZDF HD", delsys="iptv")
    b3 = Channel("Bayern 3", delsys="dvb-s2", radio=True)
    gone = Channel("Nobody TV", delsys="dvb-t2", sid=9, tsid=9, onid=9)
    calls, seen = [], []
    got = picondl.download([ard, ard2, cab, zdf, b3, gone], str(tmp_path),
                           fetcher=fake_server(calls), progress=lambda d, t: seen.append((d, t)))
    assert got == (4, 1, 1)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["1_0_1_283D_3FB_1_C00000_0_0_0.svg", "1_0_1_2B66_3F3_1_FFFF0000_0_0_0.png",
                     "bayern3.png", "zdfhd.png"]
    # each logo once, svg tried before png
    assert sum("daserstehd" in u for u in calls) == 1
    assert [u.rsplit("/", 1)[1] for u in calls if "kabeleins" in u] == \
        ["kabeleins.default.svg", "kabeleins.default.png"]
    assert seen[-1] == (4, 4)
    # the list finds them now
    folders = [str(tmp_path)]
    assert picons.find(ard, folders).endswith(".svg")
    assert picons.find(cab, folders).endswith("FFFF0000_0_0_0.png")
    assert picons.find(zdf, folders).endswith("zdfhd.png")
    # a second run has nothing to fetch but the indexes
    calls.clear()
    assert picondl.download([ard, cab, zdf, gone], str(tmp_path),
                            fetcher=fake_server(calls)) == (0, 3, 1)
    assert len(calls) == 2


def test_nothing_to_do_needs_no_network(tmp_path):
    (tmp_path / "zdfhd.png").write_bytes(b"x")
    def fail(url):
        raise AssertionError(url)
    assert picondl.download([Channel("ZDF HD", delsys="iptv")], str(tmp_path), fetcher=fail) \
        == (0, 1, 0)


def test_missing_index_is_an_error(tmp_path):
    try:
        picondl.download([Channel("X", delsys="iptv")], str(tmp_path), fetcher=lambda u: None)
    except picondl.Error:
        return
    raise AssertionError("no error")
