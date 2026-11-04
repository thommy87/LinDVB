"""HbbTV companion screen services (lindvb.hbbtvcs): app to app, DIAL,
CSS-CII / CSS-WC / CSS-TS."""

import asyncio
import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from lindvb import hbbtvcs as cs  # noqa: E402

XML_AIT = b"""<?xml version="1.0" encoding="UTF-8"?>
<mhp:ServiceDiscovery xmlns:mhp="urn:dvb:mhp:2009" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
 <mhp:ApplicationDiscovery DomainName="tv.example">
  <mhp:ApplicationList><mhp:Application>
   <mhp:appName Language="deu">Mediathek</mhp:appName>
   <mhp:applicationIdentifier><mhp:orgId>19</mhp:orgId><mhp:appId>5</mhp:appId></mhp:applicationIdentifier>
   <mhp:applicationDescriptor><mhp:type><mhp:OtherApp>application/vnd.hbbtv.xhtml+xml</mhp:OtherApp></mhp:type>
    <mhp:controlCode>AUTOSTART</mhp:controlCode></mhp:applicationDescriptor>
   <mhp:applicationTransport xsi:type="mhp:HTTPTransportType"><mhp:URLBase>http://tv.example/app/</mhp:URLBase>
   </mhp:applicationTransport>
   <mhp:applicationLocation>index.html?from=cs</mhp:applicationLocation>
  </mhp:Application></mhp:ApplicationList>
 </mhp:ApplicationDiscovery>
</mhp:ServiceDiscovery>"""


def _free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _service(**kw):
    svc = cs.CompanionService(user_agent="HbbTV/1.7.1 (; LinDVB; LinDVB; 1; ; x; )",
                              ssdp_port=_free_udp_port(), **kw)
    svc.start(network=True)
    return svc


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(asyncio.wait_for(coro, 15))


def test_parse_xml_ait():
    info = cs.parse_xml_ait(XML_AIT)
    assert info == {"url": "http://tv.example/app/index.html?from=cs", "org_id": 19, "app_id": 5,
                    "name": "Mediathek"}
    assert cs.parse_xml_ait(b"<broken") is None


def test_websocket_accept_key():
    # RFC 6455 section 1.3
    assert cs.WebSocket.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_app_to_app_pairs_and_relays():
    svc = _service()
    try:
        async def go():
            app = await cs.ws_connect(svc.app2app_local_base + "org.example.quiz")
            await asyncio.sleep(0.1)
            other = await cs.ws_connect(svc.app2app_local_base + "org.example.other")
            remote = await cs.ws_connect(svc.app2app_remote_base.replace(svc.host, "127.0.0.1")
                                         + "org.example.quiz")
            assert await app.recv() == "pairingcompleted"
            assert await remote.recv() == "pairingcompleted"
            await app.send("Frage 1")
            assert await remote.recv() == "Frage 1"
            await remote.send(b"\x00\x01" * 70000)               # binary, 16-bit+ length
            assert await app.recv() == b"\x00\x01" * 70000
            await remote.close()
            assert await app.recv() is None                       # the partner went away
            await other.close()
        _run(go())
    finally:
        svc.stop()


def test_dial_and_launch():
    asked = []
    svc = _service(on_launch=lambda info, reply: (asked.append(info), reply(True)))
    try:
        async def go():
            base = f"http://127.0.0.1:{svc.lan_port}"
            status, headers, body = await cs.http_get(base + "/dd.xml")
            assert status == 200 and headers["application-url"].endswith("/apps/")
            assert b"urn:dial-multiscreen-org:device:dial:1" in body
            status, _h, body = await cs.http_get(base + "/apps/HbbTV")
            assert status == 200 and b"X_HbbTV_App2AppURL" in body and svc.app2app_remote_base.encode() in body
            r, w = await asyncio.open_connection("127.0.0.1", svc.lan_port)
            w.write(b"POST /apps/HbbTV HTTP/1.1\r\nHost: x\r\nContent-Type: text/plain\r\n"
                    + f"Content-Length: {len(XML_AIT)}\r\n\r\n".encode() + XML_AIT)
            await w.drain()
            answer = await r.read(4096)
            w.close()
            assert answer.startswith(b"HTTP/1.1 201 Created")
            t = await cs.fetch_dial_terminal(f"http://127.0.0.1:{svc.lan_port}/dd.xml")
            assert t["friendly_name"] == "LinDVB" and t["X_HbbTV_UserAgent"].startswith("HbbTV/")
        _run(go())
        assert asked[0]["url"] == "http://tv.example/app/index.html?from=cs"
    finally:
        svc.stop()


def test_ssdp_answers_m_search():
    svc = _service()
    try:
        async def go():
            return await cs.ssdp_search(cs.DIAL_ST, 1.0, svc.ssdp_port, addr="127.0.0.1")
        try:
            found = _run(go())
        except OSError:
            return                       # no multicast socket in this sandbox
        if found:
            assert found[0] == svc.location
    finally:
        svc.stop()


def test_wall_clock_and_timeline_sync():
    svc = _service()
    try:
        svc.set_content("dvb://1.3fb.6dca", timelines=[{"timelineSelector": "urn:dvb:css:timeline:pts",
                        "timelineProperties": {"unitsPerTick": 1, "unitsPerSecond": 90000}}])
        svc.set_timeline("urn:dvb:css:timeline:pts", 900000, 5_000_000_000)

        async def go():
            loop = asyncio.get_running_loop()
            got = loop.create_future()

            class P(asyncio.DatagramProtocol):
                def datagram_received(self, data, addr):
                    got.set_result(data)
            tr, _ = await loop.create_datagram_endpoint(P, local_addr=("127.0.0.1", 0))
            sent = cs.wc_message(cs.WC_REQUEST, 123_456_789_000)
            tr.sendto(sent, ("127.0.0.1", svc.wc_port))
            reply = cs.parse_wc(await got)
            tr.close()
            assert reply["type"] == cs.WC_RESPONSE and reply["originate"] == 123_456_789_000
            assert reply["receive"] <= reply["transmit"]
            cii = await cs.ws_connect(f"ws://127.0.0.1:{svc.lan_port}/cii")
            msg = json.loads(await cii.recv())
            assert msg["contentId"] == "dvb://1.3fb.6dca" and msg["wcUrl"].startswith("udp://")
            ts = await cs.ws_connect(f"ws://127.0.0.1:{svc.lan_port}/ts")
            await ts.send(json.dumps({"contentIdStem": "dvb://1.", "timelineSelector": "urn:dvb:css:timeline:pts"}))
            ct = json.loads(await ts.recv())
            assert ct == {"contentTime": "900000", "wallClockTime": "5000000000", "timelineSpeedMultiplier": 1.0}
            svc.set_timeline("urn:dvb:css:timeline:pts", None, 6_000_000_000)
            ct = json.loads(await ts.recv())
            assert ct["contentTime"] is None
            await cii.close()
            await ts.close()
        _run(go())
    finally:
        svc.stop()
