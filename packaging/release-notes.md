## Install

**AppImage**: `chmod +x LinDVB-*-x86_64.AppImage && ./LinDVB-*-x86_64.AppImage`

**Flatpak**: `flatpak install --user LinDVB-*-x86_64.flatpak`, then `flatpak run io.github.thommy87.LinDVB`

**Debian / Ubuntu** (24.04 LTS and newer): `sudo apt install ./lindvb_*_all.deb`

**Source package**: `lindvb-*.tar.gz` is a Python sdist (with icons and translations) for
`pip install` and distribution packagers; it needs PyGObject, GTK 4 and GStreamer from the
system.

**Snap** (amd64 and arm64), from the [Snap Store](https://snapcraft.io/lindvb):

```sh
sudo snap install lindvb
sudo snap connect lindvb:dvb          # access to /dev/dvb tuners
sudo snap connect lindvb:shutdown     # optional: standby/power off after recordings
```

