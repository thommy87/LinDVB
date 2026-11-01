# DVBViewer features and how LinDVB covers them

This list was worked out from the files of a DVBViewer Pro 7.3.3.0 installation
(`Language/English.lng` declares `Version=7.3.3.0`). It reads only text resources,
configuration files, plugin names and strings. No DVBViewer code or artwork is used
in LinDVB.

Evidence references use `file:line` within the DVBViewer folder. `English.lng` is the
English UI string table, so every menu item and dialog label in the program appears
there. `actions.ini` is the list of the 202 commands that can be bound to keys or
remote buttons.

Status: **Yes** = implemented in LinDVB, **Partly** = a simpler version exists,
**No** = not implemented (reason given).

## Reception and channels

| DVBViewer feature | Evidence | LinDVB |
|---|---|---|
| Satellite, cable, terrestrial, ATSC, DTMB, ISDB reception | `Default/Transponders/` holds `DVB-S_*.ini`, `DVB-C_*.ini`, `DVB-T*_*.ini`, `ATSC_America.ini`, `DTMB.ini`, `ISDB.ini`; channel lists carry `TunerType=0/1/6` | **Yes**: GStreamer `dvbsrc` (Linux DVB API v5) with DVB-S/S2, DVB-C, DVB-T/T2, ATSC, ISDB-T, DTMB delivery systems |
| Internet TV / radio streams (HLS, DASH, RTP multicast IPTV) | `Default/Channels/6500_Internet TV (ger).ini` (`URL=…master.m3u8`), `6500_MPEG Dash (ger).ini`, `6500_MagentaTV.m3u` (`rtp://src@group:port`), `DVBViewer_IPTV_Firewall_Rules.bat` | **Yes**: playbin for http(s)/HLS/DASH/RTSP; `rtp://src@group:port` source-specific multicast mapped to `udpsrc` |
| Channel search with transponder lists | `English.lng:1205` `[frmScanner]`, `lblTransList.Caption=&Transponder Lists`, `btnScan.Caption=&Channel &Search -->` | **Yes**: runs `dvbv5-scan` (libdvbv5) with dtv-scan-tables or DVBViewer-style transponder `.ini` lists |
| Scan options: NIT search, update only, FTA only, update names, group by provider/frequency/letter | `English.lng:1240-1248` `cbNITScan`, `cbUpdateOnly`, `cbFTAOnly`, `cbUpdateNames`, `rbProvider`, `rbFrequency`, `rbInitialLetter` | **Yes** (FTA filter applies to lists that carry CA info; dvbv5 output does not) |
| LNB types and LOFs | `English.lng:1216` `cbLNBType=Manual LOF,Universal,C-Band,10700 LOF,10750 LOF,11250 LOF,11300 LOF` | **Yes**: LNB type for the scan, LOF1/LOF2/switch frequency in Preferences |
| DiSEqC switches, positioner | `English.lng:1221` `cbNetDiSEqC=None,SimpleA,SimpleB,Pos A/Opt A,…,Angular Pos.`, `btnDiSEqCEdit` | **Partly**: committed DiSEqC ports 1–4; no motor/positioner control |
| Unicable (SCR) | `English.lng:1452` `[frmUnicable]`, `cbUseSlot.Caption=Use &Unicable Slot (1..8)` | **No**: `dvbsrc` has no SCR support; would need libdvbv5 directly |
| Channel list with filters | `English.lng:1935-1957` `mnuAC3`, `mnuEncrypted`, `mnuFreetoAir`, `mnuRadio`, `mnuFavorite`, `mnuH264`, `mnuShowChannelNr` | **Yes**: TV/Radio switch (the list shows one kind; favourites mix both), All/Favourites/FTA/Encrypted/HD/Internet filters, group filter, search, channel numbers |
| Channel editor | `English.lng:542` `actChannelEdit.Caption=Channel Editor`, `[frmChannelConfig]` at line 154 | **Yes**: edit name, group, tuning data, PIDs, reorder, sort, delete, new IPTV entry |
| Favourites and favourite zapping | `English.lng:610` `actFavourites.Caption=Favourites Editor`; `actions.ini` `Favourite 0..9`, `Favourite Plus/Minus` | **Yes**: favourite flag, favourites filter, favourite plus/minus actions |
| Channel list import from other software | `Plugins/ImportCSV.dll` ("CSV Files (*.csv)"), `ImportConf.dll` ("VDR Channel CONF Files (*.conf)"), `ImportLamedb.dll` (Enigma2 lamedb) | **Yes**: CSV, VDR channels.conf, Enigma2 lamedb, plus dvbv5 files, M3U and DVBViewer `.ini` lists |
| Channel number entry, last channel, channel plus/minus | `actions.ini` `Channel 0..9`, `Last Channel=63`, `Channel Plus=9` | **Yes**: digit keys, Backspace, Page Up/Down |

## Playback

| DVBViewer feature | Evidence | LinDVB |
|---|---|---|
| Live TV and radio playback | `English.lng:1428` `[frmTVRadioOpt]`, `actRadioWindow.Caption=Radio Window` | **Yes**: TV/Radio switch that tunes the last channel of each kind; the last channel is tuned again at start |
| Visualisation for radio | `Images/Icons/VisionX/actRadioWindow.png`, visualisation plugins | **Yes**: glow waves, nebula particles, LED spectrum analyser, VU meters, oscilloscope; projectM and GOOM via the bundled GStreamer add-on; more through plugins |
| Timeshift (pause live TV, jump, keep the buffer) | `English.lng:599-601` `actTimeshift`, `actTimeshiftRecStop`, `actTimeshiftSave.Caption=Keep Timeshift File`; `actions.ini` `Jump Minus 10=102` | **Yes**: rolling TS buffer on disk, pause, ±10 s/±60 s, back to live, keep the buffer as a recording |
| Media file and URL playback, playlists | `English.lng:569-570` `actLoadFile`, `actLoadURL`; `[frmPlaylist]`; `actRandom`, `actRepeat`, `actPlaylistGo` | **Yes**: files, URLs, M3U/PLS playlists, next/previous, random, endless |
| Play rate | `English.lng:619` `mnuPlayrate.Caption=Play Rate`; `Images/Icons/VisionX/actPlayrate050..200.png` | **Yes** (files) |
| Audio track and subtitle selection | `English.lng:615,620` `mnuAudio`, `mnuSubtitle`; `[frmOptSubtitles]` line 931 | **Yes**: playbin audio/text streams, DVB subtitles, teletext subtitles (pages from the PMT teletext descriptor, decoded with `teletextdec` in the playback pipeline and drawn over the video) |
| Audio channel (stereo/left/right), second audio output | `English.lng:532-537` `ActBalanceLeft/Right`, `actAudioOutput.Caption=Audio Output B` | **No** |
| Equalizer | `English.lng:555` `actEqualizer.Caption=Equalizer...` | **Yes**: 10-band equalizer |
| Brightness, contrast, saturation, hue | `English.lng:550` `actDisplay.Caption=Brightness, Contrast...`; `actions.ini` `Brightness Up=55` … `Hue Down=62` | **Yes**: `videobalance` with reset |
| Aspect ratio, zoom, window size presets | `English.lng:606` `actZoom`, `mnuZoom.Caption=Window Size`; `actions.ini` `Window Size 50%/75%/100%` | **Yes**: fit/fill/stretch, 50/75/100/200 % |
| Full screen, always on top, desktop TV | `English.lng:549,558,578` `actDesktop`, `actFullscreen`, `actOnTop` | **Partly**: full screen and hideable channel list; GTK 4 has no always-on-top or desktop-background mode |
| Deinterlacing, pixel shaders | `Shaders/` holds 45 shader XML files (e.g. `Effect_Deinterlace.xml`, `Resample_Lanczos3.xml`, `Stereo3D_Anaglyph_Dubois.xml`); `mnuShaders` line 656 | **Partly**: deinterlace option; no shader effects |
| Screenshot | `English.lng:592` `actScreenshot.Caption=Screenshot` | **Yes**: PNG to a configurable folder |
| Picture in picture, mosaic preview | `English.lng:584,574` `actPreview`, `actMosaic`; `[frmPiP]` line 1015 | **No**: needs a second tuner pipeline and window; planned |
| Video off / audio only | `English.lng:529-530` `acDisableAV`, `actDisableV` | **Yes** |
| DVD and audio CD playback | `English.lng:552` `actDVDPlay.Caption=CD/DVD Playback`, `[frmDVDOptions]` | **No**: out of scope for a TV app |
| DirectShow filter presets | `Default/GraphPresets/*.fgp`, `Filters/PushSource.ax`, LAV Filters installer | **Not applicable**: GStreamer picks decoders automatically |

## Programme guide and teletext

| DVBViewer feature | Evidence | LinDVB |
|---|---|---|
| EPG from the broadcast | `English.lng:367` `[frmEPG]`, `tsEPG.Caption=EPG per Channel` | **Yes**: EIT parsed with GstMpegts while a channel is tuned, cached on disk |
| What's on now / at a time | `English.lng:417` `tsWhatsnow.Caption=EPG at...`, `btnSetNow` | **Yes**: Now, Next, 20:15, 22:00 |
| Timeline / TV guide grid | `English.lng:415-416` `tsTimeline`, `tsTVGuide` | **Yes**: drawn timeline with a now marker |
| EPG search (case, whole word, description) | `English.lng:409-412` `mnuSearchCase`, `mnuSearchDescription`, `mnuSearchWord` | **Yes** (case and description options) |
| Search the web for a programme | `Default/EPG_URLList.ini` (Google, IMDb, Wikipedia, TMDB…), `btnInternet` | **Yes**: configurable URL list |
| EPG export / internet EPG | `English.lng:388` `btnEPGSave.Caption=&Export EPG`; `[frmOptEPG]` | **Partly**: XMLTV import for internet channels; no export |
| EPG of other providers (MediaHighway 1/2, Freesat) | `[frmOptEPG]` MediaHighway/Freesat frequency list with Add / Delete / Default | **Yes**: frequency list in TV ▸ EPG Settings; MediaHighway 1/2 tables and Freesat (Huffman) EIT are decoded while a channel on such a transponder is tuned |
| EPG options: save to disk, preferred language, info on channel change, time zone | `[frmOptEPG]` | **Yes**: TV ▸ EPG Settings |
| Match IPTV channels to the XMLTV guide | – | **Yes**: tvg-id, then normalised names, then fuzzy matching; a dialog to check and fix the matches, which are kept |
| On-screen display: now/next, mini EPG | `OSDSkins/Default/myMiniEPG.xml`, `TVman.xml`; `TweaksPro.ini` `[20] TimeshiftEPGDisplay` | **Yes**: overlay with channel, now/next and progress |
| Teletext with page search, copy, save | `English.lng:1390` `[frmTeletext]`, `lblSearchItem`, `tbCopy.Hint`, `tbSave.Hint` | **Yes**: `teletextdec` (libzvbi) text pages, page navigation, find, copy, save |
| Teletext character sets | `English.lng:1419` `clbVTCharSet=English,Polish,…` | **Yes**: handled by libzvbi |
| HbbTV | `English.lng:1421` `gbHbbTV.Caption=HbbTV (requires add-on)` | **No** |

## Recording

| DVBViewer feature | Evidence | LinDVB |
|---|---|---|
| Instant recording | `English.lng:588` `actRecord.Caption=Record Current Channel` | **Yes** |
| Timer recordings with lead/follow-up time and weekday repeat | `English.lng:1051` `[frmRecorder]`, `lstRecorder=Channel,Date,Start,End,Name,Status,Type,Day,Duration,After Recording`, `lblLeadTime`, `lblFollowUp`, `lblMo`…`lblSo` | **Yes** |
| Record from the EPG, series recording | `English.lng:376,379` `actPVR.Caption=&Record`, `actSeries.Caption=Record Series` | **Yes**: series rules match EPG titles and add timers |
| Final action after recording | `English.lng:1076` `lblAfterRec.Caption=&Final Action:`; `cbShutdown=Close DVBViewer,Shutdown Computer,Sleep,Hibernate` | **Yes**: standby, hibernate or power off via systemd |
| File naming scheme with placeholders | `English.lng:1087` `edNameScheme.Hint=%year … %date … %time … %station … %event` | **Yes**: same placeholder idea, own implementation |
| Recording formats (TS, MPG, audio only), include teletext/subtitles/EPG | `English.lng:1078-1085` `cbTVFormat`, `cbRecTeletext`, `cbRecSubs`, `cbRecAllAudio`, `cbRecEPG` | **Partly**: always TS; includes whatever PIDs are captured (whole transponder option) |
| Recording and device statistics | `English.lng:590` `actRecorderControls.Caption=Recording and Device Statistics` | **Yes**: per-tuner lock/signal/SNR/BER/bitrate, active recordings |
| Sleep timer, stop recording after programme | `English.lng:624-633` `sr10…sr180`, `srCurrent.Caption=Stop after current programme` | **Yes** (playback sleep timer) |
| Recording database, media libraries (video, music, pictures) | `actions.ini` `Refresh RecDB=8260`; `[frmMovieLibrary]` line 819; `OSDSkins/Default/myrecordings.xml`, `mymusicfiles.xml`, `mypics.xml` | **No**: recordings are plain `.ts` files in a folder |
| Multiple tuners | `English.lng:462` `[frmHardware]` | **Yes**: list of adapters; a timer can take a tuner from live TV |

## Integration and other functions

| DVBViewer feature | Evidence | LinDVB |
|---|---|---|
| CAM / CI menu for pay TV | `English.lng:282` `[frmCIMenu]`, `Label3.Caption=This window enables communication with the common access module (CAM)` | **Yes**: CAM menu window for CI modules (EN 50221 MMI: menus, lists, PIN entry, opens by itself when the module asks); descrambling of the user's own channels via the CI module or an external softcam (DVBAPI/OSCam, libdvbcsa); see [PLUGINS.md](PLUGINS.md). No keys or card-sharing are bundled; no CI+ host authentication |
| Plugins / add-ons | `Plugins/*.dll` (ImportCSV, ImportConf, ImportLamedb, Universal), `English.lng` plugin strings | **Yes**: Python plugin system for channel-list importers, conditional-access providers, life-cycle hooks and menu entries; see [PLUGINS.md](PLUGINS.md) |
| Remote controls and key mapping | `Default/Remotes/default.remote` (XML command→key map), `Plugins/Universal.dll` (DVBSky, TeVii, Anysee, Twinhan, TechnoTrend IR), `[frmWinlircSettings]` | **Partly**: keyboard shortcuts; Linux IR receivers map to keys through the kernel's rc-core, so they work as keyboards |
| Media server client, RTSP/UPnP/web | `English.lng:1267` `[frmServiceOptions]` (DVBViewer Media Server), `RTSP Server=` line 1919, `actUPnPOpen` | **No** |
| Script hooks on events | `Scripts/channelchange.vbs`, `startrec.vbs`, `stoprec.vbs` | **No** |
| Tasks, standby/hibernate/wake-on-LAN commands | `[frmTaskOptions]`, `actions.ini` `Service Wake on LAN=8275`, `Hibernate=12323` | **Partly**: after-recording power actions |
| OSD skins, HTPC home screen, weather, news (RSS) | `OSDSkins/Default/*.xml` (`home.xml`, `myweather.xml`, `myrss.xml`), `[frmWeatherOptions]`, `[frmNewsOptions]` | **No** |
| 26 UI languages | `Language/*.lng` (Arabic … Turkish) | **No**: English only for now |
| Tweaks, key tool, support tool | `Tweaker.exe`, `TweaksPro.ini` (914 lines), `KeyTool.exe`, `SupportTool.exe` | **No** |
