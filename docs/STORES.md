# Snap Store, AUR and Flathub

The Release workflow publishes LinDVB to the GitHub release and, once their
logins are stored as repository secrets, to the Snap Store and the AUR. All
three accounts are free. Each secret is set once under
**Settings → Secrets and variables → Actions → New repository secret**.

## Snap Store (`SNAPCRAFT_STORE_CREDENTIALS`)

1. Sign in at <https://snapcraft.io/account> with an Ubuntu One account
   (create one there if needed).
2. Register the name: <https://snapcraft.io/register-snap>, name `lindvb`.
3. On a Linux computer, create a login for GitHub that may only upload LinDVB:

   ```sh
   sudo snap install snapcraft --classic
   snapcraft export-login --snaps=lindvb \
     --acls package_access,package_push,package_update,package_release -
   ```

   It asks for the Ubuntu One e-mail and password and prints a long text.
4. Store that text as the secret `SNAPCRAFT_STORE_CREDENTIALS`.

Each release then goes to the `stable` channel. The first upload, or one that
needs new permissions, can wait for a manual store review; the GitHub release
is published either way. Ask for the `dvb` interface to connect automatically
in the [Snapcraft forum](https://forum.snapcraft.io/c/store-requests/19), so
users need not run `snap connect lindvb:dvb`.

To publish an earlier release (for example one made before the secret was
set), run **Actions → Build Snap → Run workflow** with that release's tag in
"store_tag".

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
