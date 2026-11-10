# Features

Status: **Yes** = implemented in LinDVB, **Partly** = a simpler version exists,
**No** = not implemented (reason given).

## Reception and channels

| Feature | LinDVB |
|---|---|
| Satellite, cable, terrestrial, ATSC, DTMB, ISDB reception | **Yes**: GStreamer `dvbsrc` (Linux DVB API v5) with DVB-S/S2, DVB-C, DVB-T/T2, ATSC, ISDB-T, DTMB delivery systems |
| Internet TV / radio streams (HLS, DASH, RTP multicast IPTV) | **Yes**: playbin for http(s)/HLS/DASH/RTSP; HLS with separate audio renditions on playbin3 (hlsdemux2 keeps them in sync by timestamp); `rtp://src@group:port` source-specific multicast mapped to `udpsrc` |
| Channel search with transponder lists | **Yes**: runs `dvbv5-scan` (libdvbv5) with the built-in dtv-scan-tables (by country, region, provider or orbital position), blind scans, or own `.ini` and dvbv5 lists; satellites behind a DiSEqC switch searched one by one or together; transponder editor per satellite position (search single transponders; add, change, remove; own list kept per position, back to the lists in one click); lock, signal level and quality shown while searching |
| Scan options: NIT search, update only, FTA only, update names, group by provider/frequency/letter | **Yes** (FTA filter applies to lists that carry CA info; dvbv5 output does not) |
| LNB types and LOFs | **Yes**: LNB type for the scan, LOF1/LOF2/switch frequency in Preferences |
| DiSEqC switches, positioner | **Partly**: committed DiSEqC ports 1–4; no motor/positioner control |
| Unicable (SCR) | **Yes**: Unicable I (EN 50494, 8 user bands, positions A/B) and Unicable II / JESS (EN 50607, 32 user bands, 64 positions) per tuner, with user band, frequency, optional PIN and templates of common devices; LinDVB tunes these tuners itself (frontend, demux and DVR device) and sends the command again when there is no lock; channel search on the tuner too |
| SAT>IP / DVB over IP | **Yes**: SAT>IP client (RTSP SETUP/PLAY/OPTIONS/TEARDOWN, RTP over UDP or interleaved in TCP); servers found by SSDP or added by address, one tuner profile per server tuner; the stream feeds the same timeshift buffer as a local tuner (live TV, recordings, EPG, HbbTV, channel analysis), PID filter changes by PLAY `pids=`, level/lock/quality from the RTCP APP report; channel search over the server (PAT/SDT/PMT, no NIT search); DiSEqC port = SAT>IP `src` |
| Channel list with filters | **Yes**: TV/Radio switch (the list shows one kind; favourites lists mix both), All/FTA/Encrypted/HD/Internet filters plus one entry per favourites list, group filter (filter and group come back at the next start), search, channel numbers; a third "Recordings" tab lists the recordings in the recording folder (newest first, searchable, double-click plays, right-click shows the folder or deletes) |
| Channel editor | **Yes**: edit name, group, tuning data, PIDs, reorder, sort, delete, new IPTV entry; a star menu in every row to put the channel into favourites lists; edits to several channels are kept and applied together with one click |
| Favourites and favourite zapping | **Yes**: any number of named favourites lists, each sorted freely (drag and drop or right-click menu) without touching the channel numbers; numbered 1, 2, 3 … for number entry; favourite plus/minus actions and the guide's favourites view use the chosen list |
| Channel list import from other software | **Yes**: CSV, VDR channels.conf, Enigma2 lamedb, plus dvbv5 files, M3U and `.ini` channel lists |
| Channel number entry, last channel, channel plus/minus | **Yes**: digit keys, Backspace, Page Up/Down |

## Playback

| Feature | LinDVB |
|---|---|
| Live TV and radio playback | **Yes**: TV/Radio switch that tunes the last channel of each kind; at start the last channel, the first of the list, a chosen channel or none (Preferences › Channels) |
| Visualisation for radio | **Yes**: glow waves, nebula particles, light ribbons, pulsar, supernova, occasus solis, aurora borealis, Goom HD, LED spectrum analyser, VU meters, oscilloscope, drawn with OpenGL at full resolution; the original GOOM via the bundled GStreamer add-on; more through plugins; picker button in the control bar (active for radio/audio only) |
| Timeshift (pause live TV, jump, keep the buffer) | **Yes**: rolling TS buffer on disk, pause, ±10 s/±60 s, back to live, keep the buffer as a recording |
| Radio text and now-playing info in the radio window | **Yes**: RDS RadioText from the MPEG audio of DVB radio, ICY titles of internet radio and audio file tags, shown with station logo, album cover (from the file or iTunes Search) and a clock over the visualisation; Preferences › OSD and Visualisations |
| Media file and URL playback, playlists | **Yes**: files, URLs, M3U/PLS playlists, next/previous, random, endless |
| Play rate | **Yes** (files) |
| Audio track and subtitle selection | **Yes**: playbin audio/text streams with readable names ("German, Stereo", "German, 5.1"), DVB subtitles, teletext subtitles (pages from the PMT teletext descriptor, decoded with `teletextdec` in the playback pipeline and drawn over the video) |
| Audio channel (stereo/left/right), second audio output | **No** |
| Equalizer | **Yes**: 10-band equalizer |
| Brightness, contrast, saturation, hue | **Yes**: `videobalance` with reset |
| Aspect ratio, zoom, window size presets | **Yes**: fit/fill/stretch, 50/75/100/200 % |
| Full screen, always on top, desktop TV | **Partly**: full screen and hideable channel list; GTK 4 has no always-on-top or desktop-background mode |
| Deinterlacing, pixel shaders | **Partly**: deinterlace option (on the graphics card with VA-API); no shader effects |
| Screenshot | **Yes**: PNG to a configurable folder |
| Picture in picture, mosaic preview | **Partly**: picture in picture (swap, close; Menu ▸ Playback, or the channel's context menu) and a mosaic of up to nine channels per page, for internet channels; DVB channels would need a second tuner |
| Audio delay (lip sync) per channel | **Yes**: J/K or the gear menu shift the sound in 100 ms steps, saved per channel |
| Video off / audio only | **Yes** |
| DVD and audio CD playback | **No**: out of scope for a TV app |

## Programme guide and teletext

| Feature | LinDVB |
|---|---|
| EPG from the broadcast | **Yes**: EIT parsed with GstMpegts while a channel is tuned, cached on disk |
| What's on now / at a time | **Yes**: Now, Next, 20:15, 22:00 |
| Timeline / TV guide grid | **Yes**: drawn timeline with a now marker |
| EPG search (case, whole word, description) | **Yes** (case and description options) |
| Search the web for a programme | **Yes**: configurable URL list |
| EPG export / internet EPG | **Yes**: XMLTV import for internet channels; the whole guide can be exported as XMLTV (Menu ▸ File, or the button in the programme guide) |
| ATSC programme guide (PSIP) | **Yes**: virtual channel table (TVCT/CVCT) with major.minor numbers, EIT and ETT descriptions, system time table for GPS time; parsed from the raw sections |
| Closed captions (CEA-608/708) | **Yes**: `cc708overlay`, switchable in the Subtitles menu |
| EPG of other providers (MediaHighway 1/2, Freesat) | **Yes**: frequency list in Preferences ▸ EPG; MediaHighway 1/2 tables and Freesat (Huffman) EIT are decoded while a channel on such a transponder is tuned |
| Several EPG sources for one channel | **Yes**: broadcast EIT, provider EPG, DVB-I and XMLTV are kept apart and merged per channel. Automatic: now/next from the broadcast, further ahead the most complete source, gaps filled from the others, no programme twice, missing details borrowed from the same programme in another source. Preferences ▸ EPG ▸ Advanced: a custom order for all channels and per channel |
| EPG options: save to disk, preferred language, info on channel change, time zone | **Yes**: Preferences ▸ EPG |
| Match IPTV channels to the XMLTV guide | **Yes**: tvg-id, then normalised names, then fuzzy matching; a dialog to check and fix the matches, which are kept |
| On-screen display: now/next, mini EPG | **Yes**: overlay with channel, now/next and progress, genre/country/year of the programme on air, sound format, languages and subtitles on offer, and a slim quality bar (DVB: lock, SNR, signal level and uncorrected blocks of the tuner; internet: buffer and stalls of the stream) that is empty without signal and red, orange, yellow or green by quality |
| Teletext with page search, copy, save | **Yes**: `teletextdec` (libzvbi) text pages, page navigation, find, copy, save; opened from the "TXT" button next to the guide button in the header bar (greyed out when the channel has no teletext) |
| Teletext character sets | **Yes**: handled by libzvbi |
| HbbTV | **In progress** (HbbTV 2.0.4): AIT signalling, red button, OIPF objects, application lifecycle, scaled broadcast video, MPEG-DASH videos with audio languages and TTML subtitles in WebKitGTK, DSM-CC object carousel and stream events, digital teletext on TXT, youth protection with PIN, companion screens (app to app, DIAL) and media synchronisation, linked applications of DVB-I internet channels; see [HBBTV.md](HBBTV.md) for what is still missing |

## Recording

| Feature | LinDVB |
|---|---|
| Instant recording | **Yes** |
| Timer recordings with lead/follow-up time and weekday repeat | **Yes** |
| Record from the EPG, series recording | **Yes**: series rules match EPG titles and add timers |
| Final action after recording | **Yes**: standby, hibernate or power off via systemd, or quit LinDVB |
| File naming scheme with placeholders | **Yes**: same placeholder idea, own implementation |
| Recording formats (TS, MPG, MP4, MKV, native radio audio), include teletext/subtitles/all audio/EPG | **Yes**: defaults in Preferences > Recording, per timer and per instant recording ("Record with Options…"); teletext and EPG data force TS, subtitles TS or MKV (teletext subtitles as a text track); audio tracks all / preferred language (from the PMT ISO 639 descriptors, audio description skipped) / first, MP4 keeps one track; formats that cannot hold the ticked extras are greyed out; internet streams (HLS / DASH with separate audio renditions, TS or fragmented MP4 segments) are recorded in every format with video and one audio track (the preferred language when offered), their teletext / subtitle / EPG options are greyed out; no re-encoding |
| Recording and device statistics | **Yes**: per-tuner lock/signal/SNR/BER/bitrate, active recordings |
| Signal and stream analysis | **Yes**: the Channel Analysis window (seven pages) with signal margin, C/N/level/BER curves, error timeline, dish alignment tone, PID table with bit rates, CC errors and PCR jitter, transponder occupancy, video/audio formats with frame sizes and EBU R 128 loudness, SI tables with a decoded tree and hex view, clock offset, CA systems and crypto period, AIT applications; internet channels with server, HLS/DASH variants and buffer; reception log, comparison of two channels and export |
| Sleep timer, stop recording after programme | **Yes** (playback sleep timer) |
| Recording database, media libraries (video, music, pictures) | **Partly**: the Recordings tab lists the recordings with preview picture, programme details (title, subheading, channel, description from the EPG at recording time), sorting and search, and remembers where each one was left; no music or picture library |
| Multiple tuners | **Yes**: tuner list with reception types, on/off and priority; channels go to a tuner that can receive them, channels of one transponder share a tuner, tuners on one antenna cable (loop-through) keep to one band and polarisation, LNB per tuner; a timer can take a tuner from live TV |

## Integration and other functions

| Feature | LinDVB |
|---|---|
| CAM / CI menu for pay TV | **Yes**: CAM menu window for CI modules (EN 50221 MMI: menus, lists, PIN entry, opens by itself when the module asks); descrambling of the user's own channels via the CI module or an external softcam (DVBAPI/OSCam, libdvbcsa); see [PLUGINS.md](PLUGINS.md). No keys or card-sharing are bundled; no CI+ host authentication |
| Plugins / add-ons | **Yes**: Python plugin system for channel-list importers, conditional-access providers, life-cycle hooks and menu entries; see [PLUGINS.md](PLUGINS.md) |
| Remote controls and key mapping | **Partly**: keyboard shortcuts; Linux IR receivers map to keys through the kernel's rc-core, so they work as keyboards |
| Media server client, RTSP/UPnP/web | **No** |
| Script hooks on events | **Yes**: executable files in `~/.config/lindvb/hooks/` named after the event (`channel-tuned`, `recording-started`, `recording-stopped`, `reminder`, ...) get the details in `LINDVB_*` variables; see `lindvb/hooks.py` |
| Tasks, standby/hibernate/wake-on-LAN commands | **Partly**: after-recording power actions |
| OSD skins, HTPC home screen, weather, news (RSS) | **No** |
