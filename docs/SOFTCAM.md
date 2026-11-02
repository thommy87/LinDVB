# Setting up OSCam or another softcam

This guide shows how to watch the encrypted channels **you subscribe to**,
using your own subscription card and a softcam. LinDVB holds no keys. The
softcam reads your card and hands LinDVB the control words, and LinDVB
descrambles the stream with them. Card sharing, key files and emulators are
not supported and will not be.

There are two ways in:

* **OSCam (or a compatible softcam) over DVBAPI.** This is built in, so no
  plugin is needed. Sections 1 to 5 cover it.
* **Any other softcam or protocol, through a CA plugin.** Section 6 covers it.

A CI or CI+ module in the tuner needs no softcam. Choose "CI module" under
**Preferences ▸ Conditional access** (see [PLUGINS.md](PLUGINS.md)).

## 1. What you need

* A DVB tuner that works in LinDVB, and a channel list with the encrypted
  channels: from a channel search, or from DVB-I, where satellite channels are
  tuned straight from the service list.
* Your subscription card in a card reader that OSCam supports. That can be a
  PC/SC reader, a Smargo/Phoenix USB reader, or a reader on a Linux box in
  your network.
* OSCam, from your distribution or built from source.
* **libdvbcsa**, which does the actual descrambling:

  ```sh
  sudo apt install libdvbcsa1      # Debian, Ubuntu
  sudo dnf install libdvbcsa       # Fedora
  sudo pacman -S libdvbcsa         # Arch
  ```

  Without it, LinDVB connects to OSCam but the picture stays scrambled. The
  status then shows "(libdvbcsa missing)".

## 2. Configure OSCam

The files live in OSCam's config folder, often `/etc/oscam` or
`/usr/local/etc`. Only the parts that matter for LinDVB are shown here.

**oscam.conf**

```ini
[global]
logfile    = /var/log/oscam/oscam.log

[dvbapi]
enabled    = 1
au         = 1          # pass EMMs to the card (subscription updates)
boxtype    = pc         # a PC client connects to the DVBAPI socket
pmt_mode   = 6          # channel information only from the socket client
request_mode = 1
user       = lindvb     # the account below
listen_port = 0         # 0 = Unix socket /tmp/camd.socket; or e.g. 9000 for TCP

[webif]
httpport   = 8888
httpallowed = 127.0.0.1
```

**oscam.user**

```ini
[account]
user  = lindvb
group = 1
au    = 1
```

**oscam.server**: your card reader. The values depend on the reader, and the
OSCam wiki has the right ones for each model. Two common examples:

```ini
# PC/SC reader (pcscd must be running)
[reader]
label    = mycard
protocol = pcsc
device   = 0
group    = 1
emmcache = 1,3,2,0

# Smargo / Phoenix USB reader
[reader]
label    = mycard
protocol = smartreader      # or "mouse" for Phoenix-type readers
device   = Serial:XXXXXXXX  # or /dev/ttyUSB0
group    = 1
emmcache = 1,3,2,0
```

Start OSCam (`sudo systemctl start oscam`, or `oscam -b`). Then open
`http://127.0.0.1:8888` and check that the reader shows your card as
**CARDOK** and lists your subscription's entitlements.

## 3. Configure LinDVB

1. Open **☰ ▸ Preferences ▸ Conditional access**.
2. Under **Descrambling via**, choose **External softcam (DVBAPI / OSCam)**.
3. Set **DVBAPI socket**:
   * `/tmp/camd.socket` when OSCam has `listen_port = 0` on the same computer
   * `127.0.0.1:9000` (or `host:port` for another machine) when OSCam has
     `listen_port = 9000`
4. Close the preferences and tune an encrypted channel.

LinDVB connects to OSCam when you tune an encrypted channel and sends it the
channel's CA PMT. It then filters the ECM and EMM sections OSCam asks for and
applies the control words OSCam returns. The live picture, timeshift and
recordings are all descrambled.

When a softcam is chosen, LinDVB receives the **whole transponder** for
encrypted channels. The ECM PIDs are not in the channel list, so filtering by
the channel's own PIDs would hide them from OSCam. Channels without a known PMT
PID, such as DVB-I satellite channels tuned without a search, take it from the
PAT.

## 4. Check that it works

* **☰ ▸ Plugins…** shows the conditional-access status: *connecting*,
  *connected*, or *no softcam: …* with the reason (for example, the socket
  does not exist).
* The OSCam web interface shows LinDVB as a DVBAPI client. Its **Live Log**
  shows ECM requests with `found` and a response time.
* The picture appears a second or two after tuning, once the first control
  word arrives.

## 5. Troubleshooting

| What you see | Likely cause |
| --- | --- |
| *no softcam: [Errno 2] No such file or directory* | OSCam is not running, `boxtype = pc` is missing, or the socket path differs from `listen_port` / the LinDVB setting |
| *connected*, but no ECMs in the OSCam log | the channel is not marked encrypted in the channel list (run a new search), or `pmt_mode`/`user` do not match the account |
| ECMs show `not found` / `rejected` | the card is not entitled to this channel, or the reader is not `CARDOK`; check OSCam's reader page |
| picture stays scrambled although ECMs are `found` | libdvbcsa is missing (status "(libdvbcsa missing)") |
| works for a while, then stops | the card needs EMMs: set `au = 1` in `[dvbapi]` and the account, and leave the channel tuned for a few minutes |

## 6. Other softcams: a CA plugin

For a softcam with another protocol, a plugin can supply the control words.
A plugin is a Python file in `~/.local/share/lindvb/plugins/` (see
[PLUGINS.md](PLUGINS.md)). It registers a provider factory, and the name it
registers then appears under **Descrambling via** in the preferences:

```python
"""my_softcam.py – LinDVB CA provider for MySoftcam (sketch)."""

from lindvb.ca import CAProvider, CSADescrambler, parse_pmt

NAME = "MySoftcam"
VERSION = "0.1"


class MySoftcamProvider(CAProvider):
    name = "mysoftcam"

    def __init__(self, settings):
        self.settings = settings
        self.csa = CSADescrambler()       # libdvbcsa, as the built-in client uses
        self._state = "idle"

    @property
    def status(self):
        return self._state

    def start(self, channel, pmt_section=None):
        # connect to your softcam here, e.g. a socket from the settings
        self.channel = channel
        self._state = "connecting"

    def set_pmt(self, pmt_section):
        pmt = parse_pmt(pmt_section)
        # pmt.program_ca and pmt.streams carry the CA descriptors (CA system
        # IDs and ECM PIDs); send what your softcam needs. self.pmt_pid is the
        # PMT PID when the channel did not know it.
        ...

    def process_packet(self, packet):
        # pass ECM/EMM packets on to the softcam, and when it answers with a
        # control word call self.csa.set_control_word(parity, cw); then:
        return self.csa.descramble_packet(packet)

    def stop(self):
        self._state = "stopped"


def register(api):
    api.add_ca_provider("mysoftcam", MySoftcamProvider)
```

The capture calls `start` once per tuned encrypted channel and `set_pmt` as
soon as the PMT has been seen. It then calls `process_packet` for every TS
packet, which descrambles the packet in place and returns `True` when the
capture should write the packet back. Finally it calls `stop` when the channel is released. Settings the
plugin needs, such as a socket, can live in `settings.json` under the plugin's
own keys. The built-in `lindvb.ca.DvbApiProvider` is a complete example of a
provider that talks to a socket.
