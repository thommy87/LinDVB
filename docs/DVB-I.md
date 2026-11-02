# DVB-I

LinDVB can load its channel list from DVB-I (ETSI TS 103 770), following the
German profile "DTVP DVB-I Implementierungsprofil V1.1.0" (2026). Open it with
**TV ▸ DVB-I Setup…**.

## What happens

1. LinDVB shows the privacy notice. Nothing is loaded before you confirm it.
2. It asks the service list registry for Germany (`TargetCountry=DEU`). The
   DVB subdomain `deu.csr.dvbservices.org` is tried first, then
   `csr.dvb-i-slr.de`. The registries can be changed in `settings.json`
   (`dvbi_registries`).
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
     DASH instances with DRM are skipped
   * hidden services (`visible="false"`) are left out of the list but can be
     tuned by number; services that are not selectable are skipped when zapping
   * logos are downloaded to `~/.cache/lindvb/dvbi/logos`; the variant for dark
     backgrounds is preferred. A logo in your own logo folder still wins
6. The DVB-I channels come first in the channel list, in their own group. Your
   other channels keep their place after the highest DVB-I channel number.

The list is checked again at least every 24 hours (with If-Modified-Since).
When the download fails, the last list stays in use. **Update Now** in the
setup loads it at once, for example after a new channel search, and
**Remove DVB-I Channels** takes the DVB-I channels out again.

## Programme guide

DVB-I channels get their programme guide from the content guide servers named
in the service list (switch it off in the DVB-I setup). LinDVB asks for
now/next first and then the schedule of today and the next 7 days, every 6
hours. Programmes show the long synopsis, genre, age rating and the
programme image. Audio description, and subtitles on antenna/cable/satellite
channels, are marked [AD] and [ST] in the guide and can be filtered on the
Now/Next page; features LinDVB cannot play yet (subtitles in DASH, sign
language) are not listed.

## Not yet

These parts of the profile are planned for later stages:

* past days of the content guide, "more episodes" and box sets
* switching to another instance when one fails during playback, satellite
  direct tuning without a channel search, CI+ instance handling, pop-up
  channels with availability windows and out-of-service banners
* EBU-TT-D subtitles in DASH streams
* HbbTV, linked applications, MSE/EME and DRM
