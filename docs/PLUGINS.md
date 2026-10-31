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
```

The `api` object (`lindvb.plugins.PluginAPI`) is the whole surface a plugin
uses:

| Method | Purpose |
| --- | --- |
| `add_importer(label, ext, loader)` | `loader(path) -> list[Channel]` for a new channel-list format |
| `add_ca_provider(name, factory)` | `factory(settings) -> CAProvider` selectable as `ca_provider` |
| `add_hook(event, callback)` | `startup`, `channel-tuned(channel, capture)`, `shutdown` |
| `add_menu_action(label, callback)` | `callback(app)` from the Plugins menu |
| `app` / `log(msg)` | the running application, and logging |

See [`plugins/examples/hello_importer.py`](../plugins/examples/hello_importer.py)
for a working example. Plugins run inside the LinDVB process, so **only install
plugins you trust**. Manage them under **☰ → Plugins…**; disable one by adding
its file name (without `.py`) to `plugins_disabled` in `settings.json`.

## Conditional access (descrambling your own channels)

LinDVB contains **no decryption keys and breaks no encryption**. It descrambles
only with control words produced by a conditional-access client that **you** run
against **your own** subscription smart card — exactly the way tvheadend, VDR and
enigma2 do. Choose the provider under **Preferences → Conditional access**:

### External softcam (DVBAPI / OSCam) — `ca_provider = "dvbapi"`

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
tuner. The module descrambles in hardware; LinDVB opens the adapter's CA device
and sends it the CA PMT.

* `ca_ci_adapter`, `ca_ci_device` — select `/dev/dvb/adapterN/caM`.

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
