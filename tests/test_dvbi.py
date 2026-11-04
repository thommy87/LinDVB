"""DVB-I service discovery: registry, service lists, regions, LCN, instances."""

import os

import pytest

from lindvb import dvbi
from lindvb.channels import Channel, ChannelDB

REGISTRY = b"""<?xml version="1.0" encoding="UTF-8"?>
<sld:ServiceListEntryPoints xml:lang="de" xmlns:sld="urn:dvb:metadata:servicelistdiscovery:2024"
    xmlns:tva="urn:tva:metadata:2024" xmlns:sd-types="urn:dvb:metadata:servicediscovery-types:2023">
  <sld:ServiceListRegistryEntity regulatorFlag="false">
    <sld:Name>DTVP Registry</sld:Name>
  </sld:ServiceListRegistryEntity>
  <sld:ProviderOffering>
    <sld:Provider><sld:Name>Sender GmbH</sld:Name></sld:Provider>
    <sld:ServiceListOffering regulatorListFlag="false">
      <sd-types:ServiceListName>Private Liste</sd-types:ServiceListName>
      <sd-types:ServiceListURI contentType="application/xml">
        <sd-types:URI>https://example.org/private.xml</sd-types:URI>
      </sd-types:ServiceListURI>
      <sd-types:Delivery><sd-types:DASHDelivery/></sd-types:Delivery>
    </sld:ServiceListOffering>
  </sld:ProviderOffering>
  <sld:ProviderOffering>
    <sld:Provider regulatorFlag="true"><sld:Name>Medienanstalten</sld:Name></sld:Provider>
    <sld:ServiceListOffering regulatorListFlag="true">
      <sd-types:ServiceListName xml:lang="en">Germany</sd-types:ServiceListName>
      <sd-types:ServiceListName xml:lang="de">Deutschland</sd-types:ServiceListName>
      <sd-types:ServiceListURI contentType="application/xml">
        <sd-types:URI>https://example.org/de.xml</sd-types:URI>
      </sd-types:ServiceListURI>
      <sd-types:Delivery><sd-types:DASHDelivery/><sd-types:DVBTDelivery/></sd-types:Delivery>
      <sd-types:SRSSupport postcode="true"/>
      <sd-types:ServiceListId>tag:example,2026:de</sd-types:ServiceListId>
    </sld:ServiceListOffering>
  </sld:ProviderOffering>
  <sld:ProviderOffering>
    <sld:Provider><sld:Name>Radio AG</sld:Name></sld:Provider>
    <sld:ServiceListOffering>
      <sd-types:ServiceListName>Radio</sd-types:ServiceListName>
      <sd-types:ServiceListURI contentType="application/xml">
        <sd-types:URI>https://example.org/radio.xml</sd-types:URI>
      </sd-types:ServiceListURI>
    </sld:ServiceListOffering>
  </sld:ProviderOffering>
</sld:ServiceListEntryPoints>
"""

SERVICE_LIST = """<?xml version="1.0" encoding="UTF-8"?>
<ServiceList xmlns="urn:dvb:metadata:servicediscovery:2024" xml:lang="de" version="7"
    xmlns:dvbi-types="urn:dvb:metadata:servicediscovery-types:2023" xmlns:tva="urn:tva:metadata:2024">
  <Name>Deutschland</Name>
  <ProviderName>Medienanstalten</ProviderName>
  <RegionList version="1">
    <Region regionID="DE" countryCodes="DEU" selectable="false">
      <RegionName>Deutschland</RegionName>
      <Region regionID="DE-BY">
        <RegionName>Bayern</RegionName>
        <PostcodeRange from="80000" to="87999"/>
        <Region regionID="DE-BY-M"><RegionName>München</RegionName>
          <PostcodeRange from="80331" to="81929"/></Region>
      </Region>
      <Region regionID="DE-HE">
        <RegionName>Hessen</RegionName>
        <WildcardPostcode>6*</WildcardPostcode>
        <Postcode>35037</Postcode>
      </Region>
      <Region regionID="DE-BW">
        <RegionName>Baden-Württemberg</RegionName>
        <WildcardPostcode>6*</WildcardPostcode>
      </Region>
    </Region>
  </RegionList>
  <LCNTableList>
    <LCNTable version="1">
      <LCN channelNumber="1" serviceRef="tag:ard"/>
      <LCN channelNumber="2" serviceRef="tag:zdf"/>
      <LCNRange start="100" end="199"/>
    </LCNTable>
    <LCNTable version="1">
      <TargetRegion>DE-BY</TargetRegion>
      <LCN channelNumber="1" serviceRef="tag:ard"/>
      <LCN channelNumber="2" serviceRef="tag:zdf"/>
      <LCN channelNumber="3" serviceRef="tag:br"/>
      <LCN channelNumber="5" serviceRef="tag:hidden" visible="false" selectable="false"/>
    </LCNTable>
  </LCNTableList>
  <ContentGuideSource CGSID="cg1">
    <ProviderName>EPG</ProviderName>
    <ScheduleInfoEndpoint contentType="application/xml"><dvbi-types:URI>https://example.org/schedule</dvbi-types:URI></ScheduleInfoEndpoint>
    <ProgramInfoEndpoint contentType="application/xml"><dvbi-types:URI>https://example.org/program</dvbi-types:URI></ProgramInfoEndpoint>
  </ContentGuideSource>
  <Service version="1">
    <UniqueIdentifier>tag:ard</UniqueIdentifier>
    <ServiceInstance priority="1">
      <DVBTDeliveryParameters><DVBTriplet origNetId="8468" serviceId="514"/></DVBTDeliveryParameters>
    </ServiceInstance>
    <ServiceInstance priority="2">
      <DASHDeliveryParameters><UriBasedLocation contentType="application/dash+xml">
        <dvbi-types:URI>https://example.org/ard.mpd</dvbi-types:URI></UriBasedLocation></DASHDeliveryParameters>
    </ServiceInstance>
    <ServiceName xml:lang="en">The First</ServiceName>
    <ServiceName>Das Erste</ServiceName>
    <ProviderName>ARD</ProviderName>
    <RelatedMaterial>
      <tva:HowRelated href="urn:dvb:metadata:cs:HowRelatedCS:2021:1001.2"/>
      <tva:MediaLocator><tva:MediaUri contentType="image/png">https://example.org/ard_light.png</tva:MediaUri></tva:MediaLocator>
      <tva:MediaLocator><tva:MediaUri contentType="image/png">https://example.org/ard_dark.png</tva:MediaUri></tva:MediaLocator>
    </RelatedMaterial>
    <ServiceType href="urn:dvb:metadata:cs:ServiceTypeCS:2019:linear_tv"/>
    <ContentGuideSourceRef>cg1</ContentGuideSourceRef>
    <ContentGuideServiceRef>ard.de</ContentGuideServiceRef>
  </Service>
  <Service version="1">
    <UniqueIdentifier>tag:zdf</UniqueIdentifier>
    <ServiceInstance priority="2">
      <DASHDeliveryParameters><UriBasedLocation contentType="application/dash+xml">
        <dvbi-types:URI>https://example.org/zdf.mpd</dvbi-types:URI></UriBasedLocation></DASHDeliveryParameters>
    </ServiceInstance>
    <ServiceInstance priority="1">
      <ContentProtection><DRMSystemId>urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed</DRMSystemId></ContentProtection>
      <DASHDeliveryParameters><UriBasedLocation contentType="application/dash+xml">
        <dvbi-types:URI>https://example.org/zdf-drm.mpd</dvbi-types:URI></UriBasedLocation></DASHDeliveryParameters>
    </ServiceInstance>
    <ServiceName>ZDF</ServiceName>
    <ProviderName>ZDF</ProviderName>
  </Service>
  <Service version="1">
    <UniqueIdentifier>tag:br</UniqueIdentifier>
    <ServiceInstance priority="1">
      <DVBCDeliveryParameters><DVBTriplet serviceId="28107"/></DVBCDeliveryParameters>
    </ServiceInstance>
    <TargetRegion>DE-BY</TargetRegion>
    <ServiceName>BR Fernsehen</ServiceName>
    <ProviderName>ARD</ProviderName>
  </Service>
  <Service version="1">
    <UniqueIdentifier>tag:hidden</UniqueIdentifier>
    <ServiceInstance><DASHDeliveryParameters><UriBasedLocation contentType="application/dash+xml">
      <dvbi-types:URI>https://example.org/hidden.mpd</dvbi-types:URI></UriBasedLocation></DASHDeliveryParameters></ServiceInstance>
    <ServiceName>Hidden</ServiceName>
    <ProviderName>X</ProviderName>
  </Service>
  <Service version="1">
    <UniqueIdentifier>tag:radio</UniqueIdentifier>
    <ServiceInstance priority="1">
      <IdentifierBasedDeliveryParameters contentType="audio/mpeg">https://example.org/radio.mp3</IdentifierBasedDeliveryParameters>
    </ServiceInstance>
    <ServiceName>Radio Eins</ServiceName>
    <ProviderName>Radio AG</ProviderName>
    <ServiceType href="urn:dvb:metadata:cs:ServiceTypeCS:2019:linear_radio"/>
  </Service>
  <Service version="1">
    <UniqueIdentifier>tag:sat-only</UniqueIdentifier>
    <ServiceInstance priority="1">
      <DVBSDeliveryParameters><DVBTriplet origNetId="1" tsId="1019" serviceId="10301"/>
        <OrbitalPosition>19.2E</OrbitalPosition><Frequency>11494000</Frequency>
        <Polarization>horizontal</Polarization><SymbolRate>22000</SymbolRate></DVBSDeliveryParameters>
    </ServiceInstance>
    <ServiceName>Sat Only</ServiceName>
    <ProviderName>S</ProviderName>
  </Service>
</ServiceList>
""".encode()

SCANNED = [
    Channel(name="Das Erste HD", delsys="dvb-t2", frequency=482000, sid=514, tsid=4097, onid=8468,
            video_pid=101, audio_pids=[102]),
    Channel(name="BR", delsys="dvb-c-a", frequency=330000, sid=28107, tsid=1051, onid=1, encrypted=False),
    Channel(name="Wrong ONID", delsys="dvb-t2", frequency=500000, sid=514, tsid=1, onid=9999),
]


def test_registry_default_first_and_order_kept():
    offers = dvbi.parse_registry(REGISTRY)
    assert [o.name for o in offers] == ["Deutschland", "Private Liste", "Radio"]
    de = offers[0]
    assert de.default and de.official and de.postcode
    assert de.url == "https://example.org/de.xml"
    assert de.delivery == ["dvb-dash", "dvb-t"]
    assert not offers[1].default and not offers[1].official


def test_registry_query_url():
    url = dvbi.registry_query_url("https://csr.dvb-i-slr.de/query")
    assert url.startswith("https://csr.dvb-i-slr.de/query?TargetCountry=DEU&")
    assert "Delivery%5B%5D=dvb-dash" in url
    assert "regulatorListFlag" not in url


def test_registry_rejects_other_documents():
    with pytest.raises(dvbi.DVBIError):
        dvbi.parse_registry(SERVICE_LIST)


def test_parse_service_list():
    sl = dvbi.parse_service_list(SERVICE_LIST)
    assert sl.name == "Deutschland" and sl.version == 7
    ard = sl.services[0]
    assert ard.name == "Das Erste"                       # German preferred
    assert [i.kind for i in ard.instances] == ["dvb-t", "dash"]
    assert ard.instances[0].tsid is None and ard.instances[0].onid == 8468
    assert ard.logos == ["https://example.org/ard_light.png", "https://example.org/ard_dark.png"]
    assert ard.cg_source_ref == "cg1" and ard.cg_service_ref == "ard.de"
    zdf = sl.services[1]
    assert [i.priority for i in zdf.instances] == [1, 2]   # sorted by priority
    assert zdf.instances[0].drm
    radio = next(s for s in sl.services if s.uid == "tag:radio")
    assert radio.radio and radio.instances[0].kind == "icecast"
    sat = next(s for s in sl.services if s.uid == "tag:sat-only").instances[0]
    assert (sat.frequency, sat.polarization, sat.symbol_rate) == (11494000, "horizontal", 22000)
    assert sl.default_guide["schedule"] == "https://example.org/schedule"
    assert [r.id for r in sl.regions] == ["DE", "DE-BY", "DE-BY-M", "DE-HE", "DE-BW"]


def test_postcode_regions():
    sl = dvbi.parse_service_list(SERVICE_LIST)
    assert [r.id for r in dvbi.regions_for_postcode(sl, "80331")] == ["DE-BY-M"]
    assert [r.id for r in dvbi.regions_for_postcode(sl, "86150")] == ["DE-BY"]
    # wildcard 6* matches two regions: the receiver must ask (IT-5)
    assert {r.id for r in dvbi.regions_for_postcode(sl, "60311")} == {"DE-HE", "DE-BW"}
    assert [r.id for r in dvbi.regions_for_postcode(sl, "35037")] == ["DE-HE"]
    assert dvbi.regions_for_postcode(sl, "10115") == []
    assert dvbi.valid_postcode("01067") and not dvbi.valid_postcode("1234")


def test_lineup_uses_region_table_and_ranges():
    sl = dvbi.parse_service_list(SERVICE_LIST)
    by = dvbi.lineup(sl, "DE-BY-M")          # nearest table: DE-BY
    nums = {e.service.uid: e.lcn for e in by}
    assert nums["tag:ard"] == 1 and nums["tag:br"] == 3 and nums["tag:hidden"] == 5
    assert [e.lcn for e in by] == sorted(e.lcn for e in by)
    hidden = next(e for e in by if e.service.uid == "tag:hidden")
    assert not hidden.visible and not hidden.selectable
    assert nums["tag:radio"] == 6             # no range in this table: after the highest
    # without a region: the default table, BR is regional and left out,
    # unnumbered services fill the LCN range from 100
    de = dvbi.lineup(sl, "")
    nums = {e.service.uid: e.lcn for e in de}
    assert "tag:br" in nums                    # no region chosen: everything
    de_he = {e.service.uid: e.lcn for e in dvbi.lineup(sl, "DE-HE")}
    assert "tag:br" not in de_he
    assert de_he["tag:hidden"] == 100 and de_he["tag:radio"] == 101


def test_triplet_wildcards():
    inst = dvbi.Instance(kind="dvb-t", sid=514, onid=8468)        # no tsId: ignored (IT-11)
    assert dvbi.triplet_matches(inst, SCANNED[0])
    assert not dvbi.triplet_matches(inst, SCANNED[2])               # other network
    cab = dvbi.Instance(kind="dvb-c", sid=28107)                    # serviceId only (IT-12)
    assert dvbi.triplet_matches(cab, SCANNED[1])
    assert not dvbi.triplet_matches(dvbi.Instance(kind="dvb-s", sid=28107), SCANNED[1])


def test_build_channels():
    sl = dvbi.parse_service_list(SERVICE_LIST)
    res = dvbi.build_channels(sl, "DE-BY", SCANNED, "Deutschland")
    chans = {c.dvbi_id: c for c in res.channels}
    ard = chans["tag:ard"]
    assert ard.delsys == "dvb-t2" and ard.frequency == 482000 and ard.name == "Das Erste"
    assert ard.key == "dvbi:tag:ard" and ard.epg_key == "4097:514" and ard.number == 1
    assert ard.logo_url == "https://example.org/ard_dark.png"
    assert ard.root == "Deutschland" and len(ard.dvbi_instances) == 2
    zdf = chans["tag:zdf"]                     # DRM instance skipped, DASH fallback
    assert zdf.is_iptv and zdf.url == "https://example.org/zdf.mpd"
    assert zdf.epg_key == "dvbi:tag:zdf"
    assert chans["tag:br"].delsys == "dvb-c-a"
    assert chans["tag:radio"].radio and chans["tag:radio"].url.endswith(".mp3")
    assert not chans["tag:hidden"].visible
    assert [m[0] for m in res.missing] == ["Sat Only"]
    assert res.broadcast == 2 and res.internet == 3


def test_broadcast_falls_back_to_internet_without_scan():
    sl = dvbi.parse_service_list(SERVICE_LIST)
    res = dvbi.build_channels(sl, "DE-BY", [], "Deutschland")
    ard = next(c for c in res.channels if c.dvbi_id == "tag:ard")
    assert ard.is_iptv and ard.url == "https://example.org/ard.mpd"


def test_channeldb_lcn_order_and_replace(tmp_path, monkeypatch):
    monkeypatch.setattr(ChannelDB, "path", tmp_path / "channels.json")
    db = ChannelDB()
    db.merge([Channel(name="Old A", delsys="dvb-t2", frequency=1, sid=1),
              Channel(name="Old B", url="http://x", delsys="iptv")])
    assert [c.number for c in db.channels] == [1, 2]
    sl = dvbi.parse_service_list(SERVICE_LIST)
    res = dvbi.build_channels(sl, "DE-BY", SCANNED, "Deutschland")
    added, updated, removed = db.replace_dvbi(res.channels)
    assert (updated, removed) == (0, 0) and added == len(res.channels)
    assert [c.name for c in db.channels[:3]] == ["Das Erste", "ZDF", "BR Fernsehen"]
    top = max(c.lcn for c in db.channels if c.lcn)
    assert [c.number for c in db.channels if not c.dvbi_id] == [top + 1, top + 2]
    db.channels[0].favourite = True
    db.save()
    db2 = ChannelDB()
    assert db2.channels[0].dvbi_id == "tag:ard" and db2.channels[0].favourite
    # an update without ZDF keeps the favourite and removes ZDF
    again = [c for c in dvbi.build_channels(sl, "DE-BY", SCANNED, "D").channels
             if c.dvbi_id != "tag:zdf"]
    added, updated, removed = db2.replace_dvbi(again)
    assert removed == 1 and added == 0
    assert db2.by_key("dvbi:tag:ard").favourite
    assert db2.by_key("dvbi:tag:zdf") is None


class FakeNet:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, if_modified_since="", etag="", timeout=20):
        self.calls.append((url, if_modified_since))
        for prefix, answer in self.routes.items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                if callable(answer):
                    return answer(url, if_modified_since)
                return dvbi.Response(200, answer, "Mon, 02 Nov 2026 10:00:00 GMT")
        raise dvbi.DVBIError("no route " + url)


def test_query_registry_falls_back():
    net = FakeNet({"https://deu.": dvbi.DVBIError("unreachable"), "https://csr.": REGISTRY})
    offers, used = dvbi.query_registry(fetcher=net)
    assert used == "https://csr.dvb-i-slr.de/query" and offers[0].name == "Deutschland"


def test_postcode_request_and_error():
    net = FakeNet({"https://example.org/de.xml?postcode=99999":
                   dvbi.DVBIError("HTTP 400 (ERROR_INVALID_POSTCODE)", "ERROR_INVALID_POSTCODE"),
                   "https://example.org/de.xml": SERVICE_LIST})
    sl, _ = dvbi.load_service_list("https://example.org/de.xml", "80331", fetcher=net)
    assert net.calls[-1][0] == "https://example.org/de.xml?postcode=80331"
    assert sl.name == "Deutschland"
    with pytest.raises(dvbi.DVBIError) as e:
        dvbi.load_service_list("https://example.org/de.xml", "99999", fetcher=net)
    assert e.value.code == "ERROR_INVALID_POSTCODE"
    plain = FakeNet({"https://e/": lambda u, i: dvbi.Response(200, b"ERROR_INVALID_POSTCODE")})
    with pytest.raises(dvbi.DVBIError) as e:
        dvbi.load_service_list("https://e/x", "1", fetcher=plain)
    assert e.value.code == "ERROR_INVALID_POSTCODE"


def test_updater_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(ChannelDB, "path", tmp_path / "channels.json")
    state = {"modified": False}

    def servicelist(url, ims):
        if ims and not state["modified"]:
            return dvbi.Response(304)
        return dvbi.Response(200, SERVICE_LIST, "Mon, 02 Nov 2026 10:00:00 GMT")

    net = FakeNet({"https://example.org/de.xml": servicelist,
                   "https://example.org/": lambda u, i: dvbi.Response(200, b"\x89PNG logo")})
    settings = {"dvbi_list_url": "https://example.org/de.xml", "dvbi_list_name": "Deutschland",
                "dvbi_postcode": "80331", "dvbi_region": "", "dvbi_last_check": 0,
                "dvbi_last_modified": "", "dvbi_etag": ""}
    db = ChannelDB()
    up = dvbi.Updater(settings, tmp_path / "cache", tmp_path / "logos", fetcher=net)
    assert dvbi.update_due(settings, now=10**9)
    res = up.run(SCANNED)
    assert net.calls[0] == ("https://example.org/de.xml?postcode=80331", "")   # first: no header
    assert res.region == "DE-BY-M"
    msg = up.apply(res, db, now=10**9)
    assert "channels" in msg and settings["dvbi_region"] == "DE-BY-M"
    ard = db.by_key("dvbi:tag:ard")
    assert ard.logo and os.path.exists(ard.logo)
    assert not dvbi.update_due(settings, now=10**9 + 3600)
    assert dvbi.update_due(settings, now=10**9 + 24 * 3600)
    # next check: If-Modified-Since, 304 -> nothing changes
    res = up.run(SCANNED)
    assert net.calls[-1][1] == "Mon, 02 Nov 2026 10:00:00 GMT" and not res.changed
    assert "up to date" in up.apply(res, db)
    # the server fails: the last list stays (IT-18)
    up.fetcher = FakeNet({})
    res = up.run(SCANNED)
    assert res.error and "stays in use" in up.apply(res, db)
    assert db.by_key("dvbi:tag:ard") is not None
    # a forced rebuild offline uses the cached copy after a 304
    up.fetcher = net
    res = up.run([], force=True)
    assert res.changed and db.by_key("dvbi:tag:ard").delsys == "dvb-t2"
    up.apply(res, db)
    assert db.by_key("dvbi:tag:ard").is_iptv                  # no scan: internet instance


def test_updater_ambiguous_region(tmp_path):
    net = FakeNet({"https://example.org/de.xml": SERVICE_LIST})
    settings = {"dvbi_list_url": "https://example.org/de.xml", "dvbi_postcode": "60311"}
    up = dvbi.Updater(settings, tmp_path / "c", tmp_path / "l", fetcher=net)
    res = up.run(SCANNED, logos=False)
    assert {r.id for r in res.regions} == {"DE-HE", "DE-BW"}
    res = up.run(SCANNED, logos=False, region="DE-BW")
    assert res.region == "DE-BW" and not res.regions


def test_pick_logo_and_data_uri(tmp_path):
    assert dvbi.pick_logo(["a_light.png", "a_dark.png"]) == "a_dark.png"
    assert dvbi.pick_logo(["a_light.png", "a_dark.png"], dark=False) == "a_light.png"
    assert dvbi.pick_logo(["plain.png"]) == "plain.png"
    uri = "data:image/png;base64,iVBORw0KGgo="
    c = Channel(name="x", logo_url=uri)
    dvbi.download_logos([c], str(tmp_path))
    assert c.logo.endswith(".png") and open(c.logo, "rb").read().startswith(b"\x89PNG")


# ---------------------------------------------------------------- stage 3

SAT_SCANNED = SCANNED + [
    Channel(name="Some Sat", delsys="dvb-s2", root="Astra 19.2E", frequency=11494000 // 100 * 10,
            polarity="h", symbol_rate=22000, sid=1, tsid=1019, onid=1, diseqc=1),
]


def test_sat_direct_tuning():
    sl = dvbi.parse_service_list(SERVICE_LIST)
    sat = next(s for s in sl.services if s.uid == "tag:sat-only")
    inst = sat.instances[0]
    inst.frequency = 1149300                 # 11493.00 MHz in 10 kHz units
    # no satellite channels in the list: no dish, no direct tuning
    assert dvbi.direct_channel(inst, SCANNED) is None
    scanned = SCANNED + [Channel(name="Other", delsys="dvb-s", root="Astra 19.2E",
                                 frequency=12188000, polarity="h", symbol_rate=27500, sid=9,
                                 diseqc=2)]
    c = dvbi.direct_channel(inst, scanned)
    assert (c.frequency, c.polarity, c.symbol_rate, c.sid, c.diseqc, c.delsys) == \
        (11493000, "h", 22000, 10301, 2, "dvb-s2")
    assert not c.pmt_pid                   # whole transponder, service by its ID
    inst.symbol_rate = 27500
    assert dvbi.direct_channel(inst, scanned).delsys == "dvb-s"
    inst.polarization = "left circular"
    assert dvbi.direct_channel(inst, scanned) is None
    inst.polarization = "horizontal"
    ch, _ = dvbi.resolve(sat, scanned)
    assert ch.is_sat and ch.name == "Sat Only"
    assert dvbi.resolve(sat, scanned, sat_direct=False)[0] is None


def test_encrypted_instances_are_used():
    # descrambling is left to the CA provider (softcam plugin or CI module)
    inst = dvbi.Instance(kind="dvb-t", sid=514, onid=8468, ca=["0x1702"])
    dash = dvbi.Instance(kind="dash", url="https://x/a.mpd")
    found, _ = dvbi.candidates([inst, dash], SCANNED, "A")
    assert [c.delsys for _i, c in found] == ["dvb-t2", "iptv"]
    enc = [Channel(name="Enc", delsys="dvb-t2", frequency=1, sid=7, encrypted=True)]
    assert dvbi.instance_channel(dvbi.Instance(kind="dvb-t", sid=7), enc)[0].encrypted


def test_alternatives_for_failover():
    sl = dvbi.parse_service_list(SERVICE_LIST)
    ard = next(c for c in dvbi.build_channels(sl, "DE-BY", SCANNED).channels
               if c.dvbi_id == "tag:ard")
    alts = dvbi.alternatives(ard, SCANNED)
    assert len(alts) == 1
    alt = alts[0]
    assert alt.is_iptv and alt.url == "https://example.org/ard.mpd"
    assert alt.name == ard.name and alt.dvbi_id == ard.dvbi_id and alt.number == ard.number
    # the DVB-T instance is the fallback of the internet one
    web = dvbi.alternatives(alt, SCANNED)
    assert [c.delsys for c in web] == ["dvb-t2"]


AVAIL = b"""<ServiceList xmlns="urn:dvb:metadata:servicediscovery:2024" xmlns:tva="urn:tva:metadata:2024">
 <Service><UniqueIdentifier>tag:popup</UniqueIdentifier><ServiceName>EM Extra</ServiceName>
  <RelatedMaterial><tva:HowRelated href="urn:dvb:metadata:cs:HowRelatedCS:2021:1000.1"/>
   <tva:MediaLocator><tva:MediaUri contentType="image/png">https://x/off.png</tva:MediaUri></tva:MediaLocator>
  </RelatedMaterial>
  <ServiceInstance priority="1">
   <Availability>
    <Period validFrom="2026-06-01T00:00:00Z" validTo="2026-07-31T23:59:59Z">
     <Interval days="1 2 3 4 5" startTime="18:00:00Z" endTime="23:00:00Z"/>
     <Interval days="6 7" startTime="22:00:00Z" endTime="02:00:00Z"/>
    </Period>
   </Availability>
   <DASHDeliveryParameters><UriBasedLocation contentType="application/dash+xml">
    <URI>https://x/em.mpd</URI></UriBasedLocation></DASHDeliveryParameters>
  </ServiceInstance>
 </Service>
</ServiceList>"""


def _ts(text):
    from datetime import datetime
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def test_availability_windows():
    sl = dvbi.parse_service_list(AVAIL)
    svc = sl.services[0]
    assert svc.oos_logos == ["https://x/off.png"]
    per = svc.instances[0].periods
    assert dvbi.available(per, _ts("2026-06-03T19:00:00Z"))        # Wednesday evening
    assert not dvbi.available(per, _ts("2026-06-03T12:00:00Z"))    # Wednesday noon
    assert dvbi.available(per, _ts("2026-06-06T23:30:00Z"))        # Saturday late
    assert dvbi.available(per, _ts("2026-06-07T01:00:00Z"))        # after midnight, Sat night
    assert dvbi.available(per, _ts("2026-06-08T01:00:00Z"))        # Monday 1:00 = Sunday night
    assert not dvbi.available(per, _ts("2026-06-08T03:00:00Z"))    # Monday 3:00
    assert not dvbi.available(per, _ts("2026-08-03T19:00:00Z"))    # after validTo
    assert dvbi.available([], 0)
    assert dvbi.available([{"from": "2026-01-01T00:00:00Z", "to": "", "intervals": []}],
                          _ts("2026-03-01T00:00:00Z"))
    ch = dvbi.build_channels(sl, "", []).channels[0]
    assert ch.dvbi_periods == per and ch.dvbi_oos_logo == "https://x/off.png"


APPS = b"""<ServiceList xmlns="urn:dvb:metadata:servicediscovery:2024" xmlns:tva="urn:tva:metadata:2024">
 <Service><UniqueIdentifier>tag:red</UniqueIdentifier><ServiceName>Das Erste</ServiceName>
  <RelatedMaterial><tva:HowRelated href="urn:dvb:metadata:cs:LinkedApplicationCS:2019:1.1"/>
   <tva:MediaLocator><tva:MediaUri contentType="application/vnd.dvb.ait+xml">https://x/ait.xml</tva:MediaUri></tva:MediaLocator>
  </RelatedMaterial>
  <RelatedMaterial><tva:HowRelated href="urn:dvb:metadata:cs:LinkedApplicationCS:2019:1.1"/>
   <tva:MediaLocator><tva:MediaUri contentType="image/png">https://x/not-an-app.png</tva:MediaUri></tva:MediaLocator>
  </RelatedMaterial>
  <ServiceInstance priority="1">
   <RelatedMaterial><tva:HowRelated href="urn:dvb:metadata:cs:LinkedApplicationCS:2019:1.1"/>
    <tva:MediaLocator><tva:MediaUri contentType="text/html">https://x/web.html</tva:MediaUri></tva:MediaLocator>
   </RelatedMaterial>
   <DASHDeliveryParameters><UriBasedLocation contentType="application/dash+xml">
    <URI>https://x/ard.mpd</URI></UriBasedLocation></DASHDeliveryParameters>
  </ServiceInstance>
  <ServiceInstance priority="2">
   <DASHDeliveryParameters><UriBasedLocation contentType="application/dash+xml">
    <URI>https://y/ard.mpd</URI></UriBasedLocation></DASHDeliveryParameters>
  </ServiceInstance>
 </Service>
 <Service><UniqueIdentifier>tag:app</UniqueIdentifier><ServiceName>Mediathek</ServiceName>
  <ServiceInstance priority="1">
   <RelatedMaterial><tva:HowRelated href="urn:dvb:metadata:cs:LinkedApplicationCS:2019:1.2"/>
    <tva:MediaLocator><tva:MediaUri contentType="application/vnd.hbbtv.xhtml+xml">https://x/app/</tva:MediaUri></tva:MediaLocator>
   </RelatedMaterial>
   <SourceType>urn:dvb:metadata:source:application</SourceType>
  </ServiceInstance>
 </Service>
 <Service><UniqueIdentifier>tag:empty</UniqueIdentifier><ServiceName>Nichts</ServiceName>
  <ServiceInstance priority="1"><SourceType>urn:dvb:metadata:source:application</SourceType></ServiceInstance>
 </Service>
</ServiceList>"""


def test_linked_hbbtv_applications():
    sl = dvbi.parse_service_list(APPS)
    red, app, empty = sl.services
    assert red.apps == [{"url": "https://x/ait.xml", "type": "application/vnd.dvb.ait+xml",
                         "role": "parallel"}]
    assert red.instances[0].apps[0]["url"] == "https://x/web.html"
    assert app.instances[0].kind == "application"
    res = dvbi.build_channels(sl, "", [])
    assert [c.name for c in res.channels] == ["Das Erste", "Mediathek"]
    assert [m[0] for m in res.missing] == ["Nichts"]
    ard, media = res.channels
    assert [(a["url"], a["level"]) for a in ard.dvbi_apps] == \
        [("https://x/ait.xml", "service"), ("https://x/web.html", "instance")]
    # an application is the service: an internet channel without a stream
    assert media.is_iptv and media.url == "" and media.dvbi_apps[0]["role"] == "media"
    # the channel list keeps the applications
    again = Channel.from_dict(__import__("dataclasses").asdict(ard))
    assert again.dvbi_apps == ard.dvbi_apps
    # failover: the other instance has only the service's application
    alt = dvbi.alternatives(ard, [])
    assert [c.url for c in alt] == ["https://y/ard.mpd"]
    assert [a["url"] for a in alt[0].dvbi_apps] == ["https://x/ait.xml"]
