## Install

**AppImage**: `chmod +x LinDVB-*-x86_64.AppImage && ./LinDVB-*-x86_64.AppImage`

**Flatpak**: `flatpak install --user LinDVB-*-x86_64.flatpak`, then `flatpak run io.github.thommy87.LinDVB`

**Debian / Ubuntu** (24.04 LTS and newer): `sudo apt install ./lindvb_*_all.deb`

**Source package**: `lindvb-*.tar.gz` is a Python sdist (with icons and translations) for
`pip install` and distribution packagers; it needs PyGObject, GTK 4 and GStreamer from the
system.

**Snap**: this snap is not from the Snap Store, so it has no store signature. Opening it in
the software centre fails with "cannot find signatures with metadata for snap". Install it
in a terminal instead:

```sh
sudo snap install --dangerous ./lindvb_*_amd64.snap
sudo snap connect lindvb:dvb          # access to /dev/dvb tuners
sudo snap connect lindvb:shutdown     # optional: standby/power off after recordings
```

