# Plugins and conditional access

LinDVB can be extended with plugins, in the spirit of DVBViewer's add-ons, and
can descramble the encrypted channels **you are entitled to** with your own
subscription card.

## Plugins

A plugin is a Python file (or a package folder with `__init__.py`) placed in a
plugin directory:

* `~/.local/share/lindvb/plugins/` (i.e. `$XDG_DATA_HOME/lindvb/plugins`)
* any folder listed in the `LINDVB_PLUGIN_PATH` environment variable
  (`os.pathsep`-separated)

LinDVB creates `~/.local/share/lindvb/plugins/` at startup, and
*Plugins → Manage Plugins…* has an *Open Plugin Folder* button for it. The add-ons
that ship with LinDVB (such as the projectM/GOOM visualisations) live inside the
application and need nothing copied there.

Plugins are not Enigma2 packages: an `.ipk` (for example an OSCam build for a
set-top box) is not a LinDVB plugin. A softcam such as OSCam runs as its own
program, and LinDVB talks to it over the DVBAPI socket (Preferences →
Conditional access).

Each plugin defines a `register(api)` function and, optionally, `NAME` and
`VERSION`:

```python
NAME = "My importer"
VERSION = "1.0"

def register(api):
    api.add_importer("Foo lists (*.foo)", ".foo", load_foo)   # channel importer
    api.add_ca_provider("mycam", make_provider)               # CA provider
    api.add_hook("channel-tuned", on_tuned)                   # life-cycle hook
    api.add_menu_action("Do something", lambda app: ...)      # Plugins menu entry
    api.add_visualizer("My scope", draw=MyStyle)              # radio visualisation
```

The `api` object (`lindvb.plugins.PluginAPI`) is the whole surface a plugin
uses:

| Method | Purpose |
| --- | --- |
| `add_importer(label, ext, loader)` | `loader(path) -> list[Channel]` for a new channel-list format |
| `add_ca_provider(name, factory)` | `factory(settings) -> CAProvider` selectable as `ca_provider` |
| `add_hook(event, callback)` | `startup`, `channel-tuned(channel, capture)`, `shutdown` |
| `add_menu_action(label, callback)` | `callback(app)` from the Plugins menu |
| `add_visualizer(name, draw=… \| element=…, choices=…)` | a radio visualisation, see below |
| `app` / `log(msg)` | the running application, and logging |

See [`plugins/examples/hello_importer.py`](../plugins/examples/hello_importer.py)
for a working example. Plugins run inside the LinDVB process, so **only install
plugins you trust**. Manage them under **☰ → Plugins…**: each installed plugin has
an on/off switch (stored as `plugins_disabled` in `settings.json`, applied at the
next start), plugins that failed to load show the error, and plugin menu actions
can be run from there.

## Radio visualisations

While a radio channel or an audio-only file plays, LinDVB shows a visualisation
instead of the empty picture. Pick it under **☰ → Radio Visualisation** or cycle
with **V**. Built in are *Glow waves*, *Nebula particles* and *Goom HD* (modern),
the *LED spectrum analyser*, *VU meters* and an *Oscilloscope* (classic), and
*Off*, which just shows the station name.

The built-in styles are drawn on the graphics card (OpenGL 3.3 or OpenGL ES 3.0,
through the `Gtk.GLArea` GTK already provides) at the window's full resolution
and at the display's frame rate, so full screen costs almost no CPU. *Goom HD*
runs the original GOOM 2k4 logic (ported to Python in `lindvb/goom.py`: beat
detection, the eight states, the zoom filter modes and their smooth switching,
the scope lines, 3D tentacles, fireworks and the IFS fractal) at GOOM's own 30
frames per second, and draws it with shaders natively at the screen's
resolution instead of scaling up a 320x200 picture: the displacement field,
blur and fade of GOOM's zoom filter are computed per screen pixel, the IFS and
the tentacles in vertex shaders, and the lines are smooth. Without OpenGL, or with `"visualizer_gpu": false` in
`settings.json` (or the environment variable `LINDVB_NO_GPU_VIS=1`), every
style is drawn with cairo as before. A plugin can add two kinds:

* **Drawn** — `api.add_visualizer("My scope", draw=factory)`. `factory()` returns
  an object with `draw(cr, width, height, frame)`, called about 30 times a second
  with a cairo context and a `lindvb.audioviz.Frame`: `bands` (48 log-spaced
  spectrum bands, 0..1, with falling `peaks`), `level` and `vu` per channel,
  `wave` (the latest waveform, -1..1), `bass`/`mid`/`treble`, `beat`, `t`, `dt`,
  the theme `accent` colour and a `caption` (station name). The built-in styles in
  [`lindvb/visuals.py`](../lindvb/visuals.py) are written exactly this way (their
  GPU versions are in [`lindvb/ui/gpuvisuals.py`](../lindvb/ui/gpuvisuals.py));
  plugin styles are drawn with cairo.
* **GStreamer element** — `api.add_visualizer("My vis", element=factory)`.
  `factory()` returns an audio visualiser element (audio in, video out) that
  playbin renders into the video area, like `goom`, `monoscope` or `projectm`.
  With `choices=fn` the entry gets a submenu: `fn()` returns a list of variants
  (a "/" nests one under its parent, e.g. `"Dancer"`, `"Dancer/Aurora"`) and the
  picked one is passed as `factory(choice)`; "all" calls `factory()`. projectM
  uses this for its preset folders.

### projectM

LinDVB ships the add-on *GStreamer visualisations (projectM, GOOM)*
(`lindvb/addons/gst_visualizers.py`). It offers **projectM** — the open-source
MilkDrop, with thousands of presets — when the
[gst-projectm](https://github.com/projectM-visualizer/gst-projectm) GStreamer
plugin (element `projectm`, built on libprojectM 4) is installed, and **GOOM**
from gst-plugins-good. Presets are taken from `projectm_preset_dir` in
`settings.json`, or else from the usual folders such as
`/usr/share/projectM/presets`; `projectm_preset_seconds` sets how long each
preset runs (default 20). GStreamer visualisers render at 320x200 unless told
otherwise, so the add-on asks projectM (OpenGL) for the screen's resolution, at
most 2560x1440, or `visualizer_resolution` (`"1920x1080"`, default `"auto"`),
and the original GOOM, which runs on the CPU and gets slow at high resolutions,
for `goom_resolution` (default `"640x360"`). For a sharp GOOM in full screen
use the built-in *Goom HD*. Without gst-projectm the entry is simply not offered
and the plugin list says why.

The **AppImage includes projectM**: `packaging/build-projectm.sh` builds
libprojectM 4 and gst-projectm (both LGPL) and adds the *Cream of the Crop*
collection (about 9,800 presets) and the MilkDrop texture pack, so
*projectM (MilkDrop)* appears in the visualisation picker without installing
anything. Your own presets can still be used with `projectm_preset_dir`. With a
distribution package, install gst-projectm from your distribution (or build it
with that script) and a preset collection. Like any plugin, the add-on can be switched off
under **☰ → Plugins…**.

## Conditional access (descrambling your own channels)

LinDVB contains **no decryption keys and breaks no encryption**. It descrambles
only with control words produced by a conditional-access client that **you** run
against **your own** subscription smart card — exactly the way tvheadend, VDR and
enigma2 do. Choose the provider under **Preferences → Conditional access**:

### External softcam (DVBAPI / OSCam) — `ca_provider = "dvbapi"`

Step-by-step setup, OSCam configuration and troubleshooting:
[SOFTCAM.md](SOFTCAM.md).

For a card in a card reader driven by a softcam such as OSCam. OSCam reads your
subscription card, LinDVB connects to its DVBAPI socket, runs the section
filters OSCam asks for, forwards the ECM/EMM sections, and applies the control
words OSCam returns. Software descrambling uses **libdvbcsa** (install
`libdvbcsa1` / `libdvbcsa` from your distribution; without it encrypted channels
stay scrambled).

Point LinDVB at the socket your softcam exposes:

* `ca_dvbapi_socket` — a Unix socket path (OSCam default `/tmp/camd.socket`,
  configured in OSCam's `[dvbapi]` section with `boxtype = pc`), or `host:port`
  for a network socket.

### Hardware CI / CI+ module — `ca_provider = "ci"`

For a Common Interface module (with your subscription card) plugged into the
tuner. The module descrambles in hardware. LinDVB talks EN 50221 to it through the
adapter's CA device (transport and session layers, resource manager, application
information, CA support, date-time and MMI) and sends it the CA PMT of the tuned
channel.

* `ca_ci_adapter`, `ca_ci_device` — select `/dev/dvb/adapterN/caM`.

**CAM menu.** **☰ → CAM Menu (CI Module)…** opens the module's own menus: smart
card information, parental control, language and whatever else the module offers.
Menus and lists are shown as lists, questions such as a PIN prompt as an input
field (hidden for PINs). When the module asks something by itself, for example a
PIN for an age-restricted programme, the window opens on its own.

CI+ host authentication is not implemented, because it needs licensed host
certificates; CI+ modules work as far as they also operate in plain CI mode.

### None — `ca_provider = "none"` (default)

No descrambling; encrypted channels are listed but stay scrambled.

### Plugin providers

A plugin can register additional providers (for a different softcam protocol,
for example) with `add_ca_provider`; the registered name then becomes selectable
as `ca_provider`. A provider subclasses `lindvb.ca.CAProvider` and supplies
control words for the stream.

> **Scope.** This is for watching channels you subscribe to, with your own card.
> LinDVB does not bundle, and will not be extended with, decryption keys, key
> emulation, card-sharing servers, or any means of circumventing access control
> without a valid subscription.
