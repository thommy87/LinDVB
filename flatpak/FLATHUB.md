# Publishing LinDVB on Flathub

Flathub builds the Flatpak from its own repository,
`flathub/io.github.thommy87.LinDVB`, for x86_64 and aarch64, and offers updates
to everyone who installed it. The manifest there is the one in this folder, with
LinDVB itself taken from a release tag instead of the local source tree.

## First submission (once)

1. Write the manifest for the current release:
   `packaging/flathub-manifest.sh v0.X.Y` (written to `build/flathub/`).
2. Fork <https://github.com/flathub/flathub> on GitHub, **with all branches**
   (untick "Copy the master branch only").
3. In the fork, create a branch from `new-pr` and add the manifest from step 1.
4. Open a pull request against the `new-pr` branch of flathub/flathub, titled
   "Add io.github.thommy87.LinDVB".
5. Reviewers comment there. A test build starts with a `bot, build` comment.
   They usually ask about the permissions; the reasons are:
   - `--device=all`: DVB tuners are `/dev/dvb/adapter*`, which no narrower
     permission covers (the GPU alone would be `--device=dri`).
   - `--system-talk-name=org.freedesktop.login1`: optional standby or power-off
     after a timer recording.
   - `--filesystem=xdg-videos` / `xdg-pictures`: recordings and screenshots.
6. Once merged, Flathub creates `flathub/io.github.thommy87.LinDVB` and gives the
   submitter write access. The app ID `io.github.thommy87.*` is verified through
   the GitHub account `thommy87`.

## Each release afterwards

After a release has been approved and tagged:

1. `packaging/flathub-manifest.sh v0.X.Y`
2. Open a pull request in `flathub/io.github.thommy87.LinDVB` that replaces its
   manifest with the new one. Flathub's bot builds it; merging publishes it.

Keep `data/io.github.thommy87.LinDVB.metainfo.xml` up to date with a `<release>`
entry per version: Flathub shows it as the version history.
