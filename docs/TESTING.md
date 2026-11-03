# Testing without tuner hardware

## Unit tests

```sh
python3 -m pytest -q tests
```

They need Python with PyGObject; tests that need GTK, GStreamer elements or a
tuner skip themselves when those are missing.

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
sudo modprobe vidtv          # Ubuntu: needs linux-modules-extra-$(uname -r)
sudo chmod -R a+rw /dev/dvb  # or add yourself to the "video" group
```

In LinDVB, open **Preferences ▸ Tuner and LNB ▸ Channel Search** (Shift+Ctrl+S): the adapter is listed as
"Virtual tuner (vidtv)" and the transponder lists as `scantables/vidtv-…`.
Search, then play "Beethoven": you hear a tone sequence, see the radio
visualisation, the EPG fills, and recording, timeshift and timers work as with
a real tuner. Remove it again with `sudo modprobe -r vidtv`.

The automated version of this is `tests/test_vidtv.py`
(`python3 -m pytest -v tests/test_vidtv.py`); the "Virtual tuner tests"
workflow runs it on GitHub when the runner kernel has the module.

What vidtv does not cover: real reception (signal levels, lost lock),
encrypted channels, DiSEqC, video, teletext and subtitles. Those still need
a real tuner.

## Recorded transport streams

`LINDVB_TEST_TS=/path/to/file.ts lindvb` replays a recorded transport stream
instead of tuning, for any channel. Use it for video, teletext, subtitles and
the EPG of real broadcasts.
