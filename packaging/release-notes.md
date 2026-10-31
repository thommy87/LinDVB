## Install

**AppImage**: `chmod +x LinDVB-*-x86_64.AppImage && ./LinDVB-*-x86_64.AppImage`

**Flatpak**: `flatpak install --user LinDVB-*-x86_64.flatpak`, then `flatpak run io.github.thommy87.LinDVB`

**Snap**: this snap is not from the Snap Store, so it has no store signature. Opening it in
the software centre fails with "cannot find signatures with metadata for snap". Install it
in a terminal instead:

```sh
sudo snap install --dangerous ./lindvb_*_amd64.snap
sudo snap connect lindvb:dvb          # access to /dev/dvb tuners
sudo snap connect lindvb:shutdown     # optional: standby/power off after recordings
```

