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

Preferences ▸ Tuner and Channels ▸ HbbTV:

| Setting | Default | Meaning |
|---|---|---|
| Interactive services (HbbTV) | on | Watch the AIT and offer the red button |
| Start automatically | off | Start the AUTOSTART application by itself, as a TV does. Off: only with the red button |
| Subtitles in media libraries | off | `configuration.subtitlesEnabled`; DASH videos start with subtitles on |
| Audio description in media libraries | off | `configuration.audioDescriptionEnabled`; DASH videos prefer the describing soundtrack |
| Allow third-party cookies | off | Advertising networks use them to follow viewers across channels |
| Delete HbbTV Data | – | Removes cookies, local storage and cache of all applications |

Application data lives in `~/.local/share/lindvb/hbbtv` (cookies, storage)
and `~/.cache/lindvb/hbbtv`.

Needs `gir1.2-webkit-6.0` (Debian/Ubuntu) or `webkitgtk6.0` (Fedora). The
Flatpak's GNOME runtime contains it.

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
5. **Videos of the application**: MP4 and HLS play in WebKit itself. MPEG-DASH
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
starts *URL*. Replay it instead of a tuner:

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
EBU-TT-D subtitles (`tests/data/dash`).

## Still to do for HbbTV 2.0.4

| Area | What is missing |
|---|---|
| DRM | Encrypted streams (EME, Widevine/PlayReady): WebKitGTK has no such module, so pay content stays dark |
| Media | Subtitles inside DASH segments (fMP4 `stpp`) are untested; trick play for the broadcast |
| Stream events | DSM-CC stream events (`addStreamEventListener`) |
| Object carousel | Applications and files from the DSM-CC carousel (`dvb://` URLs) |
| Parental control | Rating checks and PIN |
| Teletext key | Start the digital teletext application (application_usage_descriptor 0x01) from the TXT button |
| HbbTV 2.0 extras | Companion screen and app-to-app launch (`hbbtvCSManager`), media synchroniser, `XMLHttpRequest` to `dvb:` URLs |
| Packaging | WebKitGTK in the AppImage and Snap |
