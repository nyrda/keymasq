# SteamOS / Steam Deck (AppImage)

Keymasq ships an AppImage for SteamOS and other distributions without a
native package. It is self-contained and installs itself, including the
system services and device rules Keymasq needs, and it survives SteamOS
updates.

## Install

On a Steam Deck, switch to Desktop Mode and download
`Keymasq-*-x86_64.AppImage` from the
[GitHub releases](https://github.com/nyrda/keymasq/releases).

The installer asks for your password. A factory Steam Deck has no password
set, so if you have never set one, open a terminal (Konsole) and do that
first:

```bash
passwd
```

Then make the download executable and run the installer:

```bash
cd ~/Downloads
chmod +x Keymasq-*-x86_64.AppImage
./Keymasq-*-x86_64.AppImage --install
```

The installer sets up and starts everything, including the background
services. When it finishes, launch Keymasq from the application menu, or from
the same terminal:

```bash
/opt/keymasq/bin/keymasq
```

The plain `keymasq` command works in terminals opened after your next login.

After setup, you can go back to Game Mode: remapping runs as a background
service and does not need the GUI or Desktop Mode.

The rest of this page documents what the installer puts on disk and how
updates, persistence, and uninstall behave.

## Install Model

The install lives in two places:

- `/opt/keymasq`: the application itself — the AppImage, the extracted
  runtime it runs from, and the commands under `/opt/keymasq/bin`
- `/etc`: the system integration — the `keymasqd` service, udev rules,
  macro-recording polkit rule, `/etc/keymasq/security.toml`,
  `/etc/profile.d/keymasq.sh`, and the SteamOS keep-list

The desktop entry, the `keymasq-session` user service, and the `~/.local/bin`
wrappers are installed for the invoking desktop user. The installer enables
and starts both services.

Existing commands in `~/.local/bin` are never replaced unless they are
recognizable wrappers from an earlier Keymasq AppImage install. This includes
`waypipe`; move or rename a conflicting user-managed command before installing
if you want Keymasq to create its wrapper there.

The full file layout and the runtime extraction model are documented in
`packaging/appimage/README.md`.

## SteamOS Persistence

A SteamOS update replaces the OS but does not touch Keymasq: nothing needs to
be reinstalled or re-run afterwards.

Most of the install persists on its own. On SteamOS, `/opt` lives on the same
persistent partition as `/home`, so the runtime under `/opt/keymasq` — like
your configuration in `/home` — survives OS updates without help. SteamOS
does clean third-party files out of `/etc` during updates, so the installer
registers everything it put there in a keep-list at
`/etc/atomic-update.conf.d/keymasq.conf`.

## Security Defaults

SteamOS/AppImage installs disable the recording unlock requirement in
`/etc/keymasq/security.toml`:

```toml
[recording_guard]
unlock_required = false
macro_recording_time_limit = 10
```

Recording works out of the box, without a per-session unlock.

## Remote Configuration (waypipe)

The GUI is only needed for setup, not at runtime, so on a Steam Deck you do not
have to leave Game Mode to configure Keymasq. The AppImage bundles
[`waypipe`](https://gitlab.freedesktop.org/mstoeckl/waypipe), so the Deck side
needs nothing extra — install a compatible `waypipe` on your workstation
(ideally the same version; the 0.10+ Rust series), then forward the GUI over
SSH:

```bash
waypipe -n --remote-bin /opt/keymasq/bin/waypipe ssh deck@<deck-ip> \
  /opt/keymasq/bin/keymasq
```

The GUI runs on the Deck and connects to the running `keymasq-session` there,
while it renders on your workstation's Wayland display. gamescope is not
involved: `waypipe` gives the GUI its own Wayland display, so the Deck keeps
running Game Mode untouched. This needs a Wayland compositor on your
workstation.

## Updates

Self-updates run through polkit and prompt for your password like the
installer:

```bash
keymasq --self-update
```

The update verifier uses the host `gpg` command, which SteamOS provides.

The updater downloads a JSON manifest and detached signature from the Keymasq
repository, verifies the manifest with the public key installed under
`/opt/keymasq/share/keymasq/appimage-update.gpg.asc`, verifies that the manifest
architecture matches the running system, downloads the referenced AppImage,
checks its SHA-256, extracts it into
`/opt/keymasq/runtime/<sha256>`, atomically replaces
`/opt/keymasq/Keymasq.AppImage`, atomically repoints
`/opt/keymasq/runtime/current`, refreshes installed host integration files, and
restarts services.

If either systemd service cannot be restarted, the updater exits with an error
that states the files were installed but service recovery is required; it does
not report the update as fully successful.

Signed manifest replays cannot downgrade an installed build by default: the
updater compares the manifest version with the installed Keymasq version and
refuses older versions. For an intentional rollback, use:

```bash
keymasq --self-update --allow-downgrade
```

The embedded update public key is the repository/package signing public key
published at `https://repo.keymasq.tools/gpg-key.asc`.

## Uninstall

```bash
keymasq --uninstall
```

Uninstall removes AppImage integration, systemd units, udev rules,
Keymasq-managed wrappers, desktop files, and the SteamOS keep-list. It
preserves user-managed commands with the same names. Existing input devices
are retriggered after hidden-source flags and Keymasq ACL entries are removed,
so uninstall does not require a reboot or device replug. It intentionally
leaves `/etc/keymasq`, `/var/lib/keymasq`, and user configuration/macros in
place.
