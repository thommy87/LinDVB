"""HTSP, Tvheadend's own protocol: htsmsg, login and the channel list with the
networks of the services. A small socket server stands in for Tvheadend and
checks the login like the real one does (no GTK)."""
import hashlib
import socket
import struct
import threading

import pytest

from lindvb import htsp, tvheadend
from test_tvheadend import server  # noqa: F401 - the HTTP side of the fake Tvheadend

CHALLENGE = b"0123456789abcdef0123456789abcdef"
CHANNELS = [
    {"method": "channelAdd", "channelId": 1, "channelNumber": 1, "channelName": "Das Erste HD",
     "services": [{"name": "Astra 19.2E/11494H/Das Erste HD", "type": "HDTV"},
                  {"name": "Kabel/450MHz/Das Erste HD", "type": "HDTV"}]},
    {"method": "channelAdd", "channelId": 2, "channelNumber": 2, "channelName": "ZDF HD",
     "services": [{"name": "Astra 19.2E/11362H/ZDF HD", "type": "HDTV"}]},
    {"method": "channelAdd", "channelId": 3, "channelNumber": 100, "channelName": "Bayern 3",
     "services": [{"name": "odd service name", "type": "Radio"}]},
]


def _read(sock):
    head = sock.recv(4, socket.MSG_WAITALL)
    if len(head) < 4:
        return None
    n = struct.unpack(">I", head)[0]
    return htsp.decode(sock.recv(n, socket.MSG_WAITALL)) if n else {}


class FakeHtsp:
    password = "geheim"


def _serve(sock):
    user = ""
    while (msg := _read(sock)) is not None:
        seq, method = msg.get("seq"), msg.get("method")
        if method == "hello":
            user = msg.get("username", "")
            sock.sendall(htsp.encode({"seq": seq, "htspversion": 34, "servername": "Fake",
                                      "challenge": CHALLENGE}))
        elif method == "authenticate":
            secret = FakeHtsp.password.encode() + CHALLENGE
            good = hashlib.sha1(secret, usedforsecurity=False).digest()
            sock.sendall(htsp.encode({"seq": seq} if user == "alex" and msg["digest"] == good
                                     else {"seq": seq, "noaccess": 1}))
        elif method == "enableAsyncMetadata":
            sock.sendall(htsp.encode({"seq": seq}))
            sock.sendall(htsp.encode({"method": "tagAdd", "tagId": 9, "tagName": "News"}))
            for c in CHANNELS:
                sock.sendall(htsp.encode(c))
            sock.sendall(htsp.encode({"method": "initialSyncCompleted"}))


@pytest.fixture
def fake_htsp():
    lsock = socket.create_server(("127.0.0.1", 0))

    def accept():
        while True:
            try:
                conn, _addr = lsock.accept()
            except OSError:
                return
            with conn:
                _serve(conn)
    threading.Thread(target=accept, daemon=True).start()
    yield lsock.getsockname()[1]
    lsock.close()


@pytest.mark.parametrize("value", [0, 1, 300, -1, 2 ** 40, "Grüße", b"\0\1", True,
                                   {"a": 1, "b": ["x", 2]}, ["y", {"z": b""}]])
def test_htsmsg_round_trip(value):
    data = htsp.encode({"v": value})
    assert struct.unpack(">I", data[:4])[0] == len(data) - 4
    assert htsp.decode(data[4:]) == {"v": int(value) if isinstance(value, bool) else value}


def test_htsmsg_integers_take_as_few_bytes_as_needed():
    assert htsp.encode({"n": 0})[4:] == bytes([htsp.S64, 1, 0, 0, 0, 0]) + b"n"
    assert htsp.encode({"n": 300})[-2:] == (300).to_bytes(2, "little")


def test_channels_with_login(fake_htsp):
    chans = htsp.channels("127.0.0.1", "alex", "geheim", port=fake_htsp)
    assert [c["channelName"] for c in chans] == ["Das Erste HD", "ZDF HD", "Bayern 3"]
    assert chans[0]["services"][1]["name"] == "Kabel/450MHz/Das Erste HD"


def test_wrong_password_and_no_server(fake_htsp):
    with pytest.raises(htsp.HtspError, match="wrong user name or password"):
        htsp.channels("127.0.0.1", "alex", "falsch", port=fake_htsp)
    closed = socket.create_server(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    with pytest.raises(htsp.HtspError):
        htsp.channels("127.0.0.1", port=port, timeout=2)


def test_network_of():
    assert htsp.network_of("Astra 19.2E/11494H/Das Erste HD") == "Astra 19.2E"
    assert htsp.network_of("DVB-T/K23/ZDF / 3sat") == "DVB-T"
    assert htsp.network_of("Das Erste HD") == ""


def test_load_channels_takes_the_networks_over_htsp(server, fake_htsp):  # noqa: F811
    chans = tvheadend.load_channels(server, "alex", "geheim", htsp_port=fake_htsp)
    assert [c.tvh_networks for c in chans] == [["Astra 19.2E", "Kabel"], ["Astra 19.2E"], []]


def test_load_channels_without_htsp(server, fake_htsp, monkeypatch):  # noqa: F811
    # HTSP refuses the login, or is not asked: the channels come, without networks
    monkeypatch.setattr(FakeHtsp, "password", "another one")
    for port in (fake_htsp, None):
        chans = tvheadend.load_channels(server, "alex", "geheim", htsp_port=port)
        assert [c.name for c in chans] == ["Das Erste HD", "ZDF HD", "Bayern 3"]
        assert [c.tvh_networks for c in chans] == [[], [], []]
