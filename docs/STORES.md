# Snap Store, AUR and Flathub

The Release workflow publishes LinDVB to the GitHub release and, once their
logins are stored as repository secrets, to the Snap Store and the AUR. All
three accounts are free. Each secret is set once under
**Settings → Secrets and variables → Actions → New repository secret**.

The snap is built by Canonical rather than by GitHub Actions, so the GitHub
release no longer carries a `.snap` file; it comes from the store instead.

## Snap Store

The snap is **built by Canonical**, not by GitHub Actions: snapcraft.io is
connected to this repository, so every push to `main` is built on Canonical's
build farm for the architectures under `platforms` in `snap/snapcraft.yaml`
(amd64 and arm64) and released to the `edge` channel. The builds are listed
under <https://snapcraft.io/lindvb/builds>.

That connection was made once at <https://snapcraft.io/lindvb/builds> ("Set up
build"), after registering the name at <https://snapcraft.io/register-snap>
with a free Ubuntu One account.

### Moving a release into `stable` (`SNAPCRAFT_STORE_CREDENTIALS`)

`edge` is the development channel; `snap install lindvb` installs from
`stable`. The **Snap Store** workflow moves a released version from `edge` to
`stable`, which takes seconds because nothing is built. The Release workflow
calls it after publishing the release, so a slow store cannot hold it back.

The store login is needed for that one step:

1. On a Linux computer, create a login that may only release LinDVB:

   ```sh
   sudo snap install snapcraft --classic
   snapcraft export-login --snaps=lindvb \
     --acls package_access,package_push,package_update,package_release -
   ```

   It asks for the Ubuntu One e-mail and password and prints a long text.
2. Store that text as the secret `SNAPCRAFT_STORE_CREDENTIALS`.

Without the secret the workflow only says so and does nothing. To move an
earlier release into `stable`, run **Actions → Snap Store → Run workflow**
with that release's tag.

### Interfaces the store has to allow

A snap that asks for a **super-privileged** interface is reviewed by hand, and
until that review is finished the store refuses to release it
(`resource-not-ready: Snap cannot be published due to inconsistent state.`) and
keeps the public page hidden. `shutdown` is such an interface, so the snap does
not ask for it: standby or power-off after a recording does nothing in the snap,
while the Flatpak and the AppImage still do it. To bring it back, ask for the
privilege in the
[Snapcraft forum](https://forum.snapcraft.io/c/privileged-interfaces/39) and add
`- shutdown` back to the `plugs` list once it is granted.

Two more requests are worth making in the
[forum](https://forum.snapcraft.io/c/store-requests/19), but neither blocks a
release: the `dvb` interface connecting automatically, so users need not run
`snap connect lindvb:dvb`, and `removable-media` the same.

## AUR (`AUR_SSH_PRIVATE_KEY`)

1. Create a key pair on a Linux computer, without a passphrase:

   ```sh
   ssh-keygen -t ed25519 -N "" -C "lindvb AUR" -f ~/aur_lindvb
   ```

2. Register at <https://aur.archlinux.org/register> and paste the content of
   `~/aur_lindvb.pub` into "SSH Public Key" (or later under "My Account").
3. Store the content of `~/aur_lindvb` (the private key, starting with
   `-----BEGIN OPENSSH PRIVATE KEY-----`) as the secret `AUR_SSH_PRIVATE_KEY`.

The first release afterwards creates the package
<https://aur.archlinux.org/packages/lindvb>; later releases update it.
`.github/workflows/aur.yml` builds and installs the package in an Arch Linux
container before it publishes, and can be run by hand for a released tag.

## Flathub

No secret: see [flatpak/FLATHUB.md](../flatpak/FLATHUB.md).
