# DVB-I

LinDVB can load its channel list from DVB-I (ETSI TS 103 770). Open it with
**Preferences ▸ Internet**, section DVB-I. Its settings apply at once,
without the Save button.

## Country

DVB-I is the same everywhere, but each country adds a profile on top. The
**Country** at the top of the DVB-I settings decides which registry is asked,
whether a privacy notice is shown and how your region is found
(`lindvb/dvbi_countries.py`). Each country keeps its own settings (list,
postcode, region, package); switching back brings them back. The country is
also what HbbTV applications are told (`configuration.countryId`), and the
youth protection steps in Preferences ▸ HbbTV follow it.

| | Germany | Italy | New Zealand |
|---|---|---|---|
| Profile | DTVP DVB-I Implementierungsprofil V1.1.0 | HD Forum Italia UHD Book 2.2 (A177r7, HbbTV 2.0.5) | Freeview NextGen (HbbTV 2.0.4 Annex O) |
| Registry (`TargetCountry`) | `deu.csr.dvbservices.org`, `csr.dvb-i-slr.de` (DEU) | `ita.csr.dvbservices.org`, `csr.dvbservices.org` (ITA, with the RF delivery, 7.5.1.1) | `nzl.csr.dvbservices.org`, `csr.dvbservices.org` (NZL) |
| Privacy notice | yes (IT-17) | none (the UHD Book asks for none) | none (none specified) |
| Region | postcode (5 digits) or nationwide list | TargetRegion chosen by the viewer (7.5.1.2): regione from a list (19 regions and the autonomous provinces of Trento and Bolzano), CAP optional and suggests the regione | Freeview region (Auckland, Waikato, Central, Wellington, Christchurch, Dunedin), postcode (4 digits) optional |
| Subscription package | when the list has packages | when the list has packages (7.5.1.2) | when the list has packages |
| Services without LCN | after the highest LCN | Main Overflow 850–999, then downwards from 849 (7.5.1.2) | after the highest LCN |
| Instance order | list priority (IT-10) | list priority (7.3, TS 103 770 5.2.13) | broadcast first (merged line-up) |
| Names and guide | German first | Italian first | English, then Māori |
| Youth protection steps | 6, 12, 16 | PIN from 14 and from 18 (9.2.3) | 13, 16 |

**Update the channel list automatically** can be switched off (UHD Book 7.5,
7.5.1.5); LinDVB then shows the warning the UHD Book gives and only
**Update Now** loads the list.

The fixed region (regione, Freeview region) is matched to the regions of the
service list by id (ISTAT region code, ISO 3166-2, Freeview bouquet id) or by
name; with a postcode as well, it narrows the postcode's regions.

The UHD Book gives no registry address ("https://CSR-URL/query"). The Italian
and New Zealand addresses follow the pattern of the German one and are not
confirmed; they can be changed under **Registry address** in the settings
(Enter keeps the change, empty brings back the country's defaults).

Other countries with DVB-I trials (France, Ireland, Spain, Australia) have no
published receiver profile yet.

The rest of this page describes the German profile; requirement numbers
(IT-n) refer to it.

## What happens

1. LinDVB shows the privacy notice. Nothing is loaded before you confirm it.
2. It asks the service list registry for Germany (`TargetCountry=DEU`). The
   DVB subdomain `deu.csr.dvbservices.org` is tried first, then
   `csr.dvb-i-slr.de`. The registries can be changed under **Registry
   address** (`dvbi_registries`).
3. You pick a service list. The regulator's default list comes first and is
   selected; lists flagged by the regulator are marked "official"; the other
   lists keep the registry's order.
4. You enter your postcode (or choose the nationwide list). The postcode goes
   to the service list server as `?postcode=…`. When several regions of the
   list contain the postcode, LinDVB asks which one is yours. A rejected
   postcode or another error status offers to try again or to use the
   nationwide list.
5. LinDVB builds the channel list:
   * channels are numbered by the LCN table of your region; services without
     a number get one from the table's LCN range, else after the highest LCN
   * each service can have several instances (antenna DVB-T2, cable, satellite,
     DVB-DASH, Icecast radio), tried in priority order. A broadcast instance is
     used when your own channel search found the service (DVB triplet; a missing
     tsId or origNetId is a wildcard). Otherwise the internet instance is used.
     DASH instances with DRM are skipped. A service whose instance is an
     HbbTV application (source type `application`) becomes an internet
     channel without a stream: the application presents it
   * satellite instances that your channel search did not find are tuned
     straight from the list (frequency, polarisation, symbol rate; the whole
     transponder is received and the service picked by its ID). This needs
     satellite channels in your channel list, so LinDVB knows there is a dish;
     their DiSEqC port is used. Circular polarisation is not supported. Can be
     switched off in the setup; takes effect with the next update
   * encrypted instances (ContentProtection with a CA system) are used like any
     other broadcast channel; to watch them, set up your own card in a softcam
     or a CI module ([SOFTCAM.md](SOFTCAM.md))
   * pop-up channels: an instance with an availability window is shown in the
     channel list only while it is on air. Tuned by number outside the window,
     LinDVB shows the out-of-service banner of the list instead of a picture
   * hidden services (`visible="false"`) are left out of the list but can be
     tuned by number; services that are not selectable are skipped when zapping
   * logos are downloaded to `~/.cache/lindvb/dvbi/logos`; the variant for dark
     backgrounds is preferred. A logo in your own logo folder still wins
6. The DVB-I channels come first in the channel list, in their own group. Your
   other channels keep their place after the highest DVB-I channel number.
   The group is named after the list, e.g. "DVB-I Italy · Rai" or, for a list
   loaded by its address, "DVB-I · <name of the list>". **Name in the channel
   list** in the setup sets a name of your own.
   Once you move channels in the channel editor, your order counts instead of
   the DVB-I numbers, also after list updates (new channels come at the end).
   The editor's sort menu brings back the numbers of the list.

The list is checked again at least every 24 hours (with If-Modified-Since).
When the download fails, the last list stays in use. **Update Now** in the
setup loads it at once, for example after a new channel search, and
**Remove DVB-I Channels** takes the DVB-I channels out again.

## When a stream fails (failover)

When the instance in use fails (a playback error such as HTTP 403, no free
tuner, or no signal lock for 8 seconds), LinDVB switches to the next instance
of the same channel in priority order: another internet stream, antenna,
cable or satellite. The status line names the reason and the new path. Each
instance is tried once per channel change, so two failing instances do not
alternate. Switch it off in the setup if you prefer to see the error.

## Subtitles and DASH

EBU-TT-D (TTML) subtitles in DASH streams are shown over the video and can be
chosen in the subtitle menu. They need GStreamer's TTML elements (`ttmlparse`
and `ttmlrender` from gst-plugins-bad); LinDVB gives them a rank so playbin
uses them. With them installed, the programme guide also marks subtitles [ST]
on internet channels. DASH streams with several periods and with adaptation
sets LinDVB does not know (e.g. thumbnails) play; unknown sets are ignored.

## Programme guide

DVB-I channels get their programme guide from the content guide servers named
in the service list (switch it off in the DVB-I setup). LinDVB asks for
now/next first and then the schedule of today and the next 7 days, every 6
hours. Programmes show the long synopsis, genre, age rating and the
programme image. Audio description, and subtitles on antenna/cable/satellite
channels, are marked [AD] and [ST] in the guide and can be filtered on the
Now/Next page; features LinDVB cannot play yet (subtitles in DASH, sign
language) are not listed.

For internet channels whose list entry names a content guide, this guide comes
first, even when the channel is also matched to an XMLTV channel. All other
channels keep their XMLTV (or broadcast) guide. Switching the DVB-I guide off
in the setup gives these channels their XMLTV guide back.

## Streams that do not play

Many internet streams in the German lists are meant for viewers in Germany
only. The ARD entries (`itv.ard.de/dvbi/...`) forward to the broadcaster's
stream for the viewer's country, and from abroad that server answers
"403 Forbidden". LinDVB then shows *The server refused the stream (HTTP 403)*.
This is the broadcaster's decision and cannot be fixed in the player. Channels
that only list satellite or cable, or an HbbTV application with DRM (most
private channels), are left out of the channel list.

## HbbTV on DVB-I channels

Over antenna, cable or satellite the application comes from the AIT in the
broadcast, as on any channel. For internet instances the service list names
the applications itself (`RelatedMaterial` with
`urn:dvb:metadata:cs:LinkedApplicationCS:2019`, at the service or the
instance):

* `:1.1`, an application beside the video: the red button and autostart work
  as with a broadcast AIT
* `:1.2`, an application that presents the service: it comes first, and it
  starts by itself when the service has no stream of its own

The link is an XML AIT (`application/vnd.dvb.ait+xml`, TS 102 809 5.4),
which LinDVB downloads when you tune in, or the page of the application
(HTML/XHTML). Carousel files and stream events need the broadcast, so they do
not exist on internet channels. See [HBBTV.md](HBBTV.md).

## Not yet

These parts of the profile are planned for later stages:

* past days of the content guide, "more episodes" and box sets
* direct tuning for antenna and cable (their delivery parameters carry no
  frequency, so these need a channel search)
* MSE/EME and DRM (most private channels and restart/catch-up)
