"""DVB-I country profiles: Germany, Italy (UHD Book 2.2), New Zealand (Freeview NextGen)."""

from lindvb import dvbi
from lindvb import dvbi_countries as countries
from lindvb.channels import Channel

from test_dvbi import FakeNet

IT_LIST = """<?xml version="1.0" encoding="UTF-8"?>
<ServiceList xmlns="urn:dvb:metadata:servicediscovery:2024" xml:lang="it" version="3"
    xmlns:dvbi-types="urn:dvb:metadata:servicediscovery-types:2023">
  <Name xml:lang="en">Italy</Name>
  <Name>Italia</Name>
  <ProviderName>CSR Italia</ProviderName>
  <RegionList version="1">
    <Region regionID="ITA" countryCodes="ITA" selectable="false">
      <RegionName>Italia</RegionName>
      <Region regionID="12"><RegionName>Lazio</RegionName>
        <PostcodeRange from="00010" to="04100"/></Region>
      <Region regionID="03"><RegionName>Lombardia</RegionName>
        <PostcodeRange from="20000" to="27999"/></Region>
      <Region regionID="it:bz"><RegionName>Provincia di Bolzano / Südtirol</RegionName></Region>
    </Region>
  </RegionList>
  <LCNTableList>
    <LCNTable><LCN channelNumber="5" serviceRef="tag:c5"/><LCN channelNumber="10" serviceRef="tag:local-mi"/></LCNTable>
    <LCNTable><TargetRegion>12</TargetRegion>
      <LCN channelNumber="5" serviceRef="tag:c5"/><LCN channelNumber="10" serviceRef="tag:local-rm"/></LCNTable>
  </LCNTableList>
  <Service version="1">
    <UniqueIdentifier>tag:c5</UniqueIdentifier>
    <ServiceInstance priority="1">
      <DASHDeliveryParameters><UriBasedLocation contentType="application/dash+xml">
        <dvbi-types:URI>https://example.org/c5.mpd</dvbi-types:URI></UriBasedLocation></DASHDeliveryParameters>
    </ServiceInstance>
    <ServiceInstance priority="2">
      <DVBTDeliveryParameters><DVBTriplet origNetId="318" serviceId="2"/></DVBTDeliveryParameters>
    </ServiceInstance>
    <ServiceName xml:lang="en">Channel Five</ServiceName>
    <ServiceName>Canale 5</ServiceName>
    <ProviderName>Mediaset</ProviderName>
  </Service>
  <Service version="1">
    <UniqueIdentifier>tag:local-rm</UniqueIdentifier>
    <ServiceInstance><DASHDeliveryParameters><UriBasedLocation contentType="application/dash+xml">
      <dvbi-types:URI>https://example.org/rm.mpd</dvbi-types:URI></UriBasedLocation></DASHDeliveryParameters></ServiceInstance>
    <TargetRegion>12</TargetRegion>
    <ServiceName>Roma TV</ServiceName><ProviderName>RM</ProviderName>
  </Service>
  <Service version="1">
    <UniqueIdentifier>tag:local-mi</UniqueIdentifier>
    <ServiceInstance><DASHDeliveryParameters><UriBasedLocation contentType="application/dash+xml">
      <dvbi-types:URI>https://example.org/mi.mpd</dvbi-types:URI></UriBasedLocation></DASHDeliveryParameters></ServiceInstance>
    <ServiceName>Milano TV</ServiceName><ProviderName>MI</ProviderName>
  </Service>
</ServiceList>
""".encode()

C5_DTT = Channel(name="Canale 5 HD", delsys="dvb-t2", frequency=546000, sid=2, tsid=3, onid=318,
                 video_pid=100, audio_pids=[101])


def test_profiles():
    assert [c.code for c in countries.COUNTRIES] == ["DEU", "ITA", "NZL"]
    assert countries.get("ita").name == "Italy" and countries.get("xx") is countries.GERMANY
    it, nz = countries.ITALY, countries.NEW_ZEALAND
    # 21 Italian areas: 19 regions and the autonomous provinces of Trento and Bolzano
    assert len(it.areas) == 21 and len({a.id for a in it.areas}) == 21
    assert it.consents[0].key == "privacy" and it.consents[0].required
    assert not any(c.required for c in it.consents[1:])        # optional ones are optional
    assert dvbi.valid_postcode("00184", "ITA") and not dvbi.valid_postcode("0018", it)
    assert dvbi.valid_postcode("6011", nz) and not dvbi.valid_postcode("60110", nz)
    assert dvbi.valid_postcode("80331")                          # Germany stays the default


def test_every_italian_cap_prefix_has_one_region():
    seen = {}
    for a in countries.ITALY.areas:
        for p in a.prefixes:
            assert p not in seen, (p, a.name, seen.get(p))
            seen[p] = a.name
    assert countries.area_for_postcode(countries.ITALY, "00184").name == "Lazio"
    assert countries.area_for_postcode(countries.ITALY, "39100").id == "IT-BZ"
    assert countries.area_for_postcode(countries.ITALY, "20121").name == "Lombardia"
    assert countries.area_for_postcode(countries.ITALY, "28100").name == "Piemonte"   # Novara
    assert countries.area_for_postcode(countries.NEW_ZEALAND, "6011").id == "NZ-WGN"
    assert countries.area_for_postcode(countries.NEW_ZEALAND, "1010").id == "NZ-AUK"
    assert countries.area_for_postcode(countries.NEW_ZEALAND, "9016").id == "NZ-OTA"


def test_registry_query_for_the_country():
    net = FakeNet({"https://ita.": dvbi.DVBIError("unreachable"), "https://csr.": b"x"})
    try:
        dvbi.query_registry(countries.ITALY.registries, fetcher=net, country="ITA")
    except dvbi.DVBIError:
        pass
    assert all("TargetCountry=ITA" in url for url, _ in net.calls)
    assert len(net.calls) == 2


def test_switch_country_keeps_each_countrys_settings():
    s = {"dvbi_country": "DEU", "dvbi_list_url": "https://example.org/de.xml",
         "dvbi_list_name": "Deutschland", "dvbi_postcode": "80331", "dvbi_privacy_ack": True,
         "dvbi_registries": list(countries.GERMANY.registries), "dvbi_last_check": 5,
         "hbbtv_third_party_cookies": True}
    it = countries.switch_country(s, "ITA")
    assert it is countries.ITALY and s["dvbi_country"] == s["hbbtv_country"] == "ITA"
    assert s["dvbi_list_url"] == "" and not s["dvbi_privacy_ack"] and s["dvbi_postcode"] == ""
    assert s["dvbi_registries"] == list(countries.ITALY.registries)
    assert s["dvbi_prefer_broadcast"] is True and s["dvbi_last_check"] == 0
    # consents never given count as refused: no third-party cookies, Do Not Track
    assert s["hbbtv_third_party_cookies"] is False and s["hbbtv_do_not_track"] is True
    countries.set_consent(s, it, "privacy", True, 100)
    countries.set_consent(s, it, "measurement", True, 101)
    s["dvbi_area"] = "IT-62"
    assert s["dvbi_privacy_ack"] and s["hbbtv_do_not_track"] is False
    assert countries.consent_given(s, it, "measurement")
    assert not countries.consent_given(s, it, "profiling")
    assert s["dvbi_consents"]["measurement"] == {"given": True, "time": 101}
    countries.switch_country(s, "DEU")
    assert s["dvbi_list_url"] == "https://example.org/de.xml" and s["dvbi_postcode"] == "80331"
    assert s["dvbi_privacy_ack"] and s["hbbtv_country"] == "DEU"
    countries.switch_country(s, "ITA")
    assert s["dvbi_area"] == "IT-62" and s["dvbi_consents"]["measurement"]["given"]


def test_match_area_in_service_list_regions():
    sl = dvbi.parse_service_list(IT_LIST)
    it = countries.ITALY
    assert [r.id for r in countries.match_area(sl.regions, countries.area(it, "IT-62"))] == ["12"]
    assert [r.id for r in countries.match_area(sl.regions, countries.area(it, "IT-25"))] == ["03"]
    assert [r.id for r in countries.match_area(sl.regions, countries.area(it, "IT-BZ"))] == ["it:bz"]
    assert countries.match_area(sl.regions, countries.area(it, "IT-82")) == []
    assert countries.match_area(sl.regions, None) == []


def test_italian_update_by_region_without_cap(tmp_path):
    net = FakeNet({"https://example.org/it.xml": IT_LIST})
    s = {"dvbi_country": "ITA", "dvbi_list_url": "https://example.org/it.xml",
         "dvbi_postcode": "", "dvbi_area": "IT-62", "dvbi_prefer_broadcast": True}
    try:
        res = dvbi.Updater(s, tmp_path / "c", tmp_path / "l", fetcher=net).run([C5_DTT],
                                                                                logos=False)
    finally:
        dvbi.use_country("DEU")
    assert net.calls[0][0] == "https://example.org/it.xml"          # no CAP sent
    assert res.region == "12" and not res.regions
    by_lcn = {c.lcn: c for c in res.build.channels}
    assert by_lcn[10].name == "Roma TV"                               # the Lazio table
    # Italian names first, and the terrestrial copy before the internet stream
    assert by_lcn[5].name == "Canale 5" and by_lcn[5].delsys == "dvb-t2"


def test_cap_and_region_together(tmp_path):
    net = FakeNet({"https://example.org/it.xml": IT_LIST})
    s = {"dvbi_country": "ITA", "dvbi_list_url": "https://example.org/it.xml",
         "dvbi_postcode": "20121", "dvbi_area": "IT-25"}
    try:
        res = dvbi.Updater(s, tmp_path / "c", tmp_path / "l", fetcher=net).run([], logos=False)
    finally:
        dvbi.use_country("DEU")
    assert net.calls[0][0] == "https://example.org/it.xml?postcode=20121"
    assert res.region == "03"


def test_list_priority_without_prefer_broadcast():
    sl = dvbi.parse_service_list(IT_LIST)
    c5 = next(s for s in sl.services if s.uid == "tag:c5")
    ch, inst = dvbi.resolve(c5, [C5_DTT])
    assert inst.kind == "dash"                                      # Germany: IT-10 priority
    ch, inst = dvbi.resolve(c5, [C5_DTT], prefer_broadcast=True)
    assert inst.kind == "dvb-t" and ch.delsys == "dvb-t2"
