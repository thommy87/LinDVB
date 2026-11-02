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
