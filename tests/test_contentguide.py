"""DVB-I content guide: TV-Anytime schedules and programme information."""

from lindvb import contentguide as cg
from lindvb import dvbi
from lindvb.channels import Channel
from lindvb.epg import EPGStore

T0 = 1793577600          # 2026-11-02T00:00:00Z

SCHEDULE = b"""<?xml version="1.0" encoding="UTF-8"?>
<TVAMain xmlns="urn:tva:metadata:2024" xmlns:mpeg7="urn:tva:mpeg7:2008" xml:lang="de">
 <ProgramDescription>
  <ProgramInformationTable>
   <ProgramInformation programId="crid://ard.de/tagesschau">
    <BasicDescription>
     <Title type="main">Tagesschau</Title>
     <Title type="main" xml:lang="en">News</Title>
     <Synopsis length="short">Nachrichten</Synopsis>
     <Synopsis length="long">Die Nachrichten
       des Tages.</Synopsis>
     <Genre href="urn:dvb:metadata:cs:ContentSubject:2019:2" type="main"/>
     <ParentalGuidance><mpeg7:MinimumAge>0</mpeg7:MinimumAge></ParentalGuidance>
     <RelatedMaterial>
      <HowRelated href="urn:tva:metadata:cs:HowRelatedCS:2012:19"/>
      <MediaLocator><MediaUri contentType="image/jpeg">https://img.example/ts.jpg</MediaUri></MediaLocator>
     </RelatedMaterial>
    </BasicDescription>
    <AVAttributes>
     <AudioAttributes><AudioLanguage purpose="urn:x">deu</AudioLanguage></AudioAttributes>
     <AudioAttributes><AudioPurpose href="urn:tva:metadata:cs:AudioPurposeCS:2007:1"/></AudioAttributes>
     <CaptionLanguage closed="true">deu</CaptionLanguage>
     <SignLanguage>gsg</SignLanguage>
    </AVAttributes>
   </ProgramInformation>
   <ProgramInformation programId="crid://ard.de/film">
    <BasicDescription>
     <Title type="main">Der Film</Title>
     <Title type="secondary">Teil 2</Title>
     <Synopsis length="medium">Ein Film.</Synopsis>
     <ParentalGuidance><mpeg7:MinimumAge>12</mpeg7:MinimumAge></ParentalGuidance>
    </BasicDescription>
   </ProgramInformation>
  </ProgramInformationTable>
  <ProgramLocationTable>
   <Schedule serviceIDRef="ard.de" start="2026-11-02T20:00:00Z" end="2026-11-02T22:00:00Z">
    <ScheduleEvent>
     <Program crid="crid://ard.de/tagesschau"/>
     <PublishedStartTime>2026-11-02T20:00:00Z</PublishedStartTime>
     <PublishedDuration>PT15M</PublishedDuration>
    </ScheduleEvent>
    <ScheduleEvent>
     <Program crid="crid://ard.de/film"/>
     <PublishedStartTime>2026-11-02T21:15:00+01:00</PublishedStartTime>
     <PublishedDuration>PT1H30M</PublishedDuration>
    </ScheduleEvent>
    <ScheduleEvent>
     <Program crid="crid://ard.de/unknown"/>
     <PublishedStartTime>2026-11-02T21:45:00Z</PublishedStartTime>
     <PublishedDuration>PT15M</PublishedDuration>
    </ScheduleEvent>
   </Schedule>
  </ProgramLocationTable>
 </ProgramDescription>
</TVAMain>
"""

PROGRAM = b"""<TVAMain xmlns="urn:tva:metadata:2024" xml:lang="de"><ProgramDescription>
 <ProgramInformationTable><ProgramInformation programId="crid://ard.de/unknown">
  <BasicDescription><Title>Wetter</Title><Synopsis length="short">Das Wetter.</Synopsis></BasicDescription>
 </ProgramInformation></ProgramInformationTable></ProgramDescription></TVAMain>"""


def test_urls():
    assert cg.now_next_url("https://g/s", "ard.de") == "https://g/s?sid=ard.de&now_next=true"
    assert cg.schedule_url("https://g/s?x=1", "a", 10, 20) == "https://g/s?x=1&sid=a&start=10&end=20"
    assert cg.program_url("https://g/p", "crid://a/b") == "https://g/p?pid=crid%3A%2F%2Fa%2Fb"
    assert cg.image_url("https://i/x.jpg") == "https://i/x.jpg?w=640"
    assert cg.image_url("https://i/x.jpg?w=320") == "https://i/x.jpg?w=320"


def test_times():
    assert cg.parse_time("2026-11-02T00:00:00Z") == T0
    assert cg.parse_time("2026-11-02T01:00:00+01:00") == T0
    assert cg.parse_time("2026-11-02T00:00:00.000Z") == T0
    assert cg.parse_duration("PT1H30M") == 5400
    assert cg.parse_duration("P1DT2S") == 86402
    assert cg.parse_time("bad") is None


def test_parse_tva():
    programmes, items = cg.parse_tva(SCHEDULE)
    ts = programmes["crid://ard.de/tagesschau"]
    assert ts.title == "Tagesschau"                       # German preferred
    assert ts.synopsis == "Die Nachrichten des Tages."    # long synopsis
    assert ts.genre == "News/Current affairs"
    assert ts.image == "https://img.example/ts.jpg"
    assert ts.access == [cg.AUDIO_DESCRIPTION, cg.SUBTITLES, cg.SIGN_LANGUAGE]
    assert [i.service for i in items] == ["ard.de"] * 3
    film = items[1].event
    assert film.start == T0 + 20 * 3600 + 15 * 60 and film.duration == 5400
    assert film.subtitle == "Teil 2" and film.min_age == 12
    assert items[2].event.title == "" and items[2].event.programme_id == "crid://ard.de/unknown"


def test_supported_access():
    acc = [cg.AUDIO_DESCRIPTION, cg.SUBTITLES, cg.SIGN_LANGUAGE]
    assert cg.supported_access(acc, broadcast=False) == [cg.AUDIO_DESCRIPTION]
    assert cg.supported_access(acc, broadcast=True) == [cg.AUDIO_DESCRIPTION, cg.SUBTITLES]


def test_sources_for_channels():
    a = Channel(name="ARD", delsys="iptv", url="https://x/a.mpd", dvbi_id="tag:ard",
                dvbi_cg="ard.de", dvbi_guide={"schedule": "https://g/s", "program": "https://g/p"})
    b = Channel(name="no guide", delsys="iptv", url="https://x/b", dvbi_id="tag:b")
    c = Channel(name="M3U", delsys="iptv", url="https://x/c")
    srcs = cg.sources_for([a, b, c])
    assert len(srcs) == 1
    assert (srcs[0].key, srcs[0].sid, srcs[0].broadcast) == ("dvbi:tag:ard", "ard.de", False)
    a.dvbi_cg = ""
    assert cg.sources_for([a])[0].sid == "tag:ard"       # UniqueIdentifier otherwise


def test_day_spans():
    spans = cg.day_spans(T0 + 3600)
    assert spans[0] == (T0, T0 + cg.DAY) and len(spans) == 8
    assert spans[-1][1] == T0 + 8 * cg.DAY


def test_load_guide_and_store(tmp_path, monkeypatch):
    calls = []

    def net(url, if_modified_since="", etag="", timeout=20):
        calls.append(url)
        if url.startswith("https://g/p"):
            return dvbi.Response(200, PROGRAM)
        return dvbi.Response(200, SCHEDULE)

    src = cg.GuideSource("dvbi:tag:ard", "ard.de", "https://g/s", "https://g/p")
    events, errors = cg.load_guide(src, fetcher=net, now=T0, days=False)
    assert not errors
    assert calls[0] == "https://g/s?sid=ard.de&now_next=true"
    assert any("pid=crid" in u for u in calls)
    wetter = next(e for e in events if e.programme_id == "crid://ard.de/unknown")
    assert wetter.title == "Wetter" and wetter.description == "Das Wetter."
    ts = events[0]
    assert ts.access == [cg.AUDIO_DESCRIPTION]           # internet: no subtitles/signing yet
    calls.clear()
    events, _ = cg.load_guide(src, fetcher=net, now=T0, details=False)
    assert sum("start=" in u for u in calls) == 8
    monkeypatch.setattr(EPGStore, "path", tmp_path / "epg.json")
    store = EPGStore()
    for e in events:
        store.add(src.key, e)
    assert [e.title for e in store.for_key(src.key, since=T0)][:2] == ["Tagesschau", "Der Film"]
    store.save()
    again = EPGStore()
    # the saved file keeps the DVB-I fields (only future events are loaded)
    assert all(hasattr(e, "access") for evs in again.events.values() for e in evs.values())
    assert cg.badges(ts) == "[AD]"


def test_load_guide_reports_errors():
    def net(url, **_kw):
        raise dvbi.DVBIError("HTTP 404")

    events, errors = cg.load_guide(cg.GuideSource("k", "s", "https://g/s"), fetcher=net, now=T0)
    assert events == [] and errors


def test_build_channels_sets_guide():
    from test_dvbi import SCANNED, SERVICE_LIST
    sl = dvbi.parse_service_list(SERVICE_LIST)
    res = dvbi.build_channels(sl, "DE-BY", SCANNED, "D")
    ard = next(c for c in res.channels if c.dvbi_id == "tag:ard")
    assert ard.dvbi_guide["schedule"] == "https://example.org/schedule"
    assert ard.dvbi_cg == "ard.de"
