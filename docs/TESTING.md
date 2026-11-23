# Testing without tuner hardware

## Unit tests

```sh
python3 -m pytest -q tests
```

They need Python with PyGObject; tests that need GTK, GStreamer elements or a
tuner skip themselves when those are missing. The player tests need a display
(`xvfb-run -a python3 -m pytest -q tests` without one) and, for the stream
samples, `x264enc` (gstreamer1.0-plugins-ugly).

## The regression net for streams, timeshift, subtitles and extra audio

`tests/test_regression_net.py` pins down how each reception path behaves
today: HLS with redundant rendition groups (3sat), WebVTT without time stamps
(PBS), a live server that stops answering, DASH with roles (ONE HD), CEA-708
captions in the picture, HbbTV of DVB-I channels, the channel search and
where recordings go. The streams are made on the fly by `tests/streamkit.py`
and served from 127.0.0.1, so no network is needed.

Bugs seen on air that are not fixed yet are tests marked `known_bug(...)`
(`xfail(strict=True)`, failing only on an assertion). They are reported as
"xfailed" while the bug is there. A change that fixes one makes it "XPASS",
which fails the run: remove the marker in the same change, so every fix is
visible here and nothing is fixed (or broken) by accident. A test without
the marker that starts failing is a regression of a path that worked.

## Player resources

`tools/perfcheck.py` measures the player: CPU per second of picture, peak
memory, frames reaching the sink and late frames for a 720p HLS stream, the
same without the closed caption chain and with CEA-708 captions shown, the
timeshift reader alone, and threads, open files and memory after many
channel changes.

```sh
xvfb-run -a tools/perfcheck.py run --out head.json             # this checkout
xvfb-run -a tools/perfcheck.py run --lindvb ../base --out base.json
tools/perfcheck.py compare base.json head.json
```

The numbers are relative: they depend on the machine, so compare two runs on
the same one. The "Player resources" workflow does that on every pull request
that touches the player (base and head on one runner) and puts the table in
the job summary; threads or open files that grow with every channel change
fail it. On a machine with a hardware decoder, `--decoder auto` measures the
path users get.

## The virtual tuner (vidtv)

Since Linux 5.10 the kernel has a virtual DVB driver, **vidtv**. It shows up as
a normal DVB adapter (`/dev/dvb/adapterN`) and sends one radio service,
"Beethoven" (provider LinuxTV.org), with PAT, PMT, SDT and EIT. It accepts:

| Delivery system | Frequency | Scan file in LinDVB |
|---|---|---|
| DVB-T / DVB-T2 | 474 MHz | `scantables/vidtv-dvbt` |
| DVB-C | 474 MHz, 6940 kBd | `scantables/vidtv-dvbc` |
| DVB-S / DVB-S2 | 11362 MHz (universal LNB) | `scantables/vidtv-dvbs` |

Load it and allow your user to open it:

```sh
sudo modprobe vidtv          # Ubuntu: needs linux-modules-extra-$(uname -r);
                             # cloud kernels without it: see .github/workflows/vidtv.yml
sudo chmod -R a+rw /dev/dvb  # or add yourself to the "video" group
```

In LinDVB, open **Preferences ▸ Tuner and Channels ▸ Channel Search** (Shift+Ctrl+S): the adapter is listed as
"Virtual tuner (vidtv)"; pick the country "Virtual tuner (vidtv)" (cable and
terrestrial) or 19.2°E (satellite) to search with the `scantables/vidtv-…` lists.
Search, then play "Beethoven": you hear a tone sequence, see the radio
visualisation, the EPG fills, and recording, timeshift and timers work as with
a real tuner. Remove it again with `sudo modprobe -r vidtv`.

The automated version of this is `tests/test_vidtv.py`
(`python3 -m pytest -v tests/test_vidtv.py`); the "Virtual tuner tests"
workflow runs it on GitHub; when the runner kernel lacks the module it builds
vidtv from the matching mainline sources, and it fails rather than skip.

What vidtv does not cover: real reception (signal levels, lost lock),
encrypted channels, DiSEqC, video, teletext and subtitles. Those still need
a real tuner.

## Recorded transport streams

`LINDVB_TEST_TS=/path/to/file.ts lindvb` replays a recorded transport stream
instead of tuning, for any channel. Use it for video, teletext, subtitles and
the EPG of real broadcasts.
