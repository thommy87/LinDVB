# HbbTV in LinDVB

LinDVB is becoming an HbbTV 2.0.4 terminal (ETSI TS 102 796 v1.7.1). The
channel's interactive application runs in WebKitGTK 6, in a transparent view
over the live picture, and talks to LinDVB through the OIPF objects in
`lindvb/hbbtv.js`.

## Using it

* **Red button:** F5 on the keyboard, or the red key of a remote control
  (`XF86Red`). The **HbbTV** button in the header bar and *TV ▸ HbbTV (red
  button)* do the same. The button appears when the channel signals an
  application.
* **Colour keys:** F5 red, F6 green, F7 yellow, F8 blue (`XF86Green`,
  `XF86Yellow`, `XF86Blue` from a remote).
* **Arrows, Enter, Backspace (back), digits, media keys** go to the
  application while it asks for them (its key set); otherwise LinDVB uses them
  as usual.
* **Esc** closes the application. It stays closed on this channel until you
  press red again or change the channel.
* **TXT** (Ctrl+Shift+T, the TXT button) opens the channel's digital teletext
  application where the AIT signals one, instead of the classic teletext.
  TXT again goes back to TV.

Preferences ▸ Tuner and Channels ▸ HbbTV:

| Setting | Default | Meaning |
|---|---|---|
| Interactive services (HbbTV) | on | Watch the AIT and offer the red button |
| Start automatically | off | Start the AUTOSTART application by itself, as a TV does. Off: only with the red button |
| Subtitles in media libraries | off | `configuration.subtitlesEnabled`; DASH videos start with subtitles on |
| Audio description in media libraries | off | `configuration.audioDescriptionEnabled`; DASH videos prefer the describing soundtrack |
| Teletext from the interactive service | on | TXT opens the HbbTV teletext of the channel where there is one |
| Youth protection | off | The age of the youngest viewer (6, 12, 16). Applications and DASH videos rated above it need the PIN |
| Current PIN / New PIN | – | The youth protection PIN. Once set, changing the age or the PIN needs the current PIN |
| Companion screens on the network | off | DIAL discovery, app to app with phones and tablets, inter-device sync, starting applications from a companion. Off: only the local app-to-app endpoint on 127.0.0.1 runs |
| Allow third-party cookies | off | Advertising networks use them to follow viewers across channels |
| Delete HbbTV Data | – | Removes cookies, local storage and cache of all applications |

Application data lives in `~/.local/share/lindvb/hbbtv` (cookies, storage)
and `~/.cache/lindvb/hbbtv`.

Needs `gir1.2-webkit-6.0` (Debian/Ubuntu) or `webkitgtk6.0` (Fedora). The
Flatpak's GNOME runtime contains it, the Snap takes it from the GNOME content
snap, and the AppImage brings its own (with the web, network and GPU processes;
`packaging/build-appdir.sh` points libwebkitgtk at them relative to the
AppImage, and CI checks that the AppImage's web process starts).

WebKit runs every application in a bubblewrap sandbox, which needs
unprivileged user namespaces. Ubuntu 24.04 and later forbid them for programs
without an AppArmor profile, and WebKit would then end LinDVB as soon as an
application starts. LinDVB checks this at start and switches HbbTV off with a
hint in the preferences. To allow it, either install the Flatpak, or allow user
namespaces (`sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0`, or an
AppArmor profile for LinDVB with `userns,`).

Youth protection: an application whose AIT entry is rated above the age never
starts by itself; with the red button LinDVB asks for the PIN. A DASH video
whose manifest carries a rating above the age (`<Rating
schemeIdUri="urn:dvb:iptv:rating:2014">`) is held back until the PIN is
entered in LinDVB's dialog or the application checks it with
`verifyParentalControlPIN()`. Three wrong PINs lock the PIN for five minutes.
Applications cannot switch the protection off. The live broadcast itself is not
blocked; LinDVB reports its ratings to applications. The protection keeps
children from starting content by accident; anyone who can edit LinDVB's
settings file can switch it off.

## How it works

1. **Signalling** (`lindvb/ait.py`): the PMT announces the AIT on a PID with
   stream_type 0x05 and an application_signalling_descriptor (0x6F). The
   capture watches PAT, PMT and AIT (table 0x74, several sections, version
   changes) and adds the AIT PID to the demux filter while running. The PID is
   stored in the channel list (`ait_pid`) and kept in TS recordings.
2. **Lifecycle** (`lindvb/hbbtv.py`, `Lifecycle`): which application runs.
   AUTOSTART apps start (or wait for red), KILL/DESTROY end them, apps not
   signalled on a new service end, service-bound apps end on a channel change,
   `createApplication()` starts broadcast-independent apps.
3. **Browser** (`lindvb/ui/hbbtv.py`): a WebKitGTK view over the 16:9 video
   area, zoomed so the application sees 1280×720. Documents served as
   `application/vnd.hbbtv.xhtml+xml` are taken as a download and loaded as
   HTML at their own URL (WebKit does not show that type itself). The user
   agent carries `HbbTV/1.7.1`.
4. **OIPF objects** (`lindvb/hbbtv.js`, injected before the page):
   `application/oipfApplicationManager` (application, key set, show/hide,
   destroy, createApplication), `application/oipfConfiguration`,
   `application/oipfCapabilities`, `video/broadcast` (bind, set channel, channel
   list, programmes now/next, full screen or a window of its own; LinDVB scales
   the picture into that window; the live audio and subtitle tracks as
   components the app can switch), the A/V control object, `oipfObjectFactory`,
   `KeyEvent.VK_*`.
5. **Object carousel** (`lindvb/dsmcc.py`): applications signalled with the
   DSM-CC transport (protocol 0x0001) are loaded from the broadcast. LinDVB
   adds the carousel's PID to the demux filter, puts DSI, DII and DDB sections
   (CRC-checked, compressed modules too) into modules, reads the BIOP
   directories, files and stream event objects, and answers `dvb://` URLs
   from them (TS 102 851: `dvb://current.<component tag>/path` or
   `dvb://<onid>.<tsid>.<sid>.<component tag>/path`). Pages, images, scripts
   and XHRs wait up to 30 seconds for a file that has not come round yet.
6. **Stream events**: `video/broadcast.addStreamEventListener()` with a stream
   event object of the carousel or an XML event description (over HTTP or from
   the carousel). LinDVB watches the stream the event is on (table 0x3D) and
   calls the listener once for each new version of the stream event
   descriptor, with the private data as `data` (hex) and `text`. A channel
   change ends all listeners with status `error`.
7. **Companion screens** (`lindvb/hbbtvcs.py`, TS 102 796 clause 14):
   `application/hbbtvCSManager` gives applications the app-to-app base URLs,
   the inter-device sync URL and the DIAL launch URL, and finds other HbbTV
   terminals on the network (`discoverTerminals`). The app-to-app server pairs
   an application's connection to `ws://127.0.0.1:<port>/hbbtv/<name>` with a
   companion's connection to `ws://<LAN address>:<port>/hbbtv/<name>` and
   relays their messages. With the network setting on, LinDVB answers SSDP
   searches for DIAL, describes itself (`dd.xml`, `apps/HbbTV` with
   `X_HbbTV_App2AppURL`, `X_HbbTV_InterDevSyncURL`, `X_HbbTV_UserAgent`)
   and lets a companion start a broadcast-independent application with an XML
   AIT, after asking the user.
8. **Media synchroniser** (clause 13, TS 103 286-2): `createMediaSynchroniser()`
   / `application/hbbtvMediaSynchroniser` with `video/broadcast` (timeline
   `urn:dvb:css:timeline:pts`, measured from the PTS of the live video in the
   transport stream) or an application video (`urn:dvb:css:timeline:ct`,
   `urn:dvb:css:timeline:mpd:period:rel:<ticks>`) as master. `addMediaObject()`
   keeps one application video in step with the master by seeking and small
   playback rate changes. `enableInterDeviceSync()` starts CSS-CII (content
   id `dvb://onid.tsid.sid`), CSS-WC (UDP wall clock) and CSS-TS (control
   timestamps) for companion screens.
9. **Videos of the application**: MP4 and HLS play in WebKit itself. MPEG-DASH
   (the media libraries) plays through [dash.js](https://github.com/Dash-Industry-Forum/dash.js)
   4.7.4 (`lindvb/vendor`, BSD licence) on Media Source Extensions, in HTML5
   `<video>` elements (`src`, `setAttribute`, `<source type="application/dash+xml">`)
   and in A/V control objects. dash.js is loaded into the page without a global
   name, so an application's own copy is not disturbed, and stores nothing in
   the application's storage. Audio languages and TTML / EBU-TT-D subtitles are
   components (`getComponents`, `selectComponent`); subtitles are drawn in a
   layer over the video. While an application plays its own video, LinDVB
   stops the live picture and sound, and brings them back on
   `bindToCurrentChannel()` or when the application ends.

## Testing without DVB hardware

`tools/hbbtv-test-ts.py URL -o hbbtv.ts` writes a transport stream whose AIT
starts *URL*. With `--carousel DIR` the application comes from an object
carousel built from *DIR* (URL is then the entry page inside it), `--event
quiz=1` adds a stream event object `events/stream`, and `--fire 1:Frage` sends
that event every few seconds. Replay it instead of a tuner:

```sh
python3 -m http.server 8000 &          # serves your test application
tools/hbbtv-test-ts.py http://localhost:8000/index.html --sid 28106 -o hbbtv.ts
LINDVB_TEST_TS=hbbtv.ts LINDVB_HBBTV_DEBUG=1 lindvb
```

Tune a channel with that service id. `LINDVB_HBBTV_DEBUG=1` writes the
application's console messages to the terminal and enables the WebKit
inspector. A recording of a real channel in TS format also works, since it
keeps the AIT.

`tests/test_hbbtv_browser.py` runs applications end to end in WebKitGTK
(under `xvfb-run`): the red button, the key set, the video window, live
components, a DASH `<video>`, a DASH A/V object with two audio languages and
EBU-TT-D subtitles (`tests/data/dash`), an application from an object carousel
with stream events, and a rated DASH video behind the PIN.

## Still to do for HbbTV 2.0.4

| Area | What is missing |
|---|---|
| DRM | Encrypted streams (EME, Widevine/PlayReady): WebKitGTK has no such module, so pay content stays dark |
| Media | Subtitles inside DASH segments (fMP4 `stpp`) are untested; trick play for the broadcast |
| Stream events | Scheduled events (by NPT time) fire on arrival like do-it-now events |
| Object carousel | Carousels of other services (remote `dvb://` URLs with another service id) |
| Companion screens | `discoverCSLaunchers()` finds no launchers (no CS launcher application registers with LinDVB), so `launchCSApp()` reports an unknown launcher. Starting applications from a companion (DIAL) works |
| Media synchroniser | The broadcast PTS is taken where LinDVB demultiplexes, corrected by the pipeline latency, so it is accurate to about a frame or two, not to the millisecond. One application video per synchroniser; LinDVB cannot be a slave terminal (`initSlaveMediaSynchroniser`); TEMI timelines are not read |
| Packaging | The Snap's HbbTV relies on WebKitGTK 6 in the GNOME 46 content snap; it is only built for releases and not tested in CI |
