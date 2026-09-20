# SteamOS / Steam Deck (AppImage)

Keymasq ships an AppImage for SteamOS and other distributions without a
native package. The AppImage bundles its own runtime and installs itself,
along with the system services and device rules Keymasq needs. SteamOS
updates do not remove the install.

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

After setup, you can go back to Game Mode. Remapping runs as a background
service and does not need the GUI or Desktop Mode.

The rest of this page covers what the installer puts on disk, how the install
persists across SteamOS updates, and how to update and uninstall Keymasq.

## Install model

The installer writes to two system locations:

- `/opt/keymasq` holds the application. That is the AppImage, the extracted
  runtime it runs from, and the commands under `/opt/keymasq/bin`.
- `/etc` holds the system integration. That is the `keymasqd` service, udev
  rules, macro-recording polkit rule, `/etc/keymasq/security.toml`,
  `/etc/profile.d/keymasq.sh`, and the SteamOS keep-list.

The installer also sets up the desktop entry, the `keymasq-session` user
service, and the `~/.local/bin` wrappers for the desktop user who ran it. It
then enables and starts both services.

The installer only replaces a command in `~/.local/bin` if it recognizes that
command as a wrapper from an earlier Keymasq AppImage install. This applies to
`waypipe` too. If you have your own command with a conflicting name and want
Keymasq to create its wrapper there, move or rename yours before installing.

`packaging/appimage/README.md` documents the full file layout and the runtime
extraction model.

## SteamOS persistence

A SteamOS update replaces the OS but leaves Keymasq in place. You do not need
to reinstall or re-run anything afterwards.

On SteamOS, `/opt` is on the same persistent partition as `/home`. OS updates
leave that partition alone, so the runtime under `/opt/keymasq` and your
configuration in `/home` both survive them. SteamOS does delete third-party
files from `/etc` during updates. To prevent that, the installer lists every
file it put there in a keep-list at `/etc/atomic-update.conf.d/keymasq.conf`.

## Security defaults

SteamOS/AppImage installs disable the recording unlock requirement in
`/etc/keymasq/security.toml`:

```toml
[recording_guard]
unlock_required = false
macro_recording_time_limit = 10
```

With this setting, you can record macros without unlocking recording in each
session.

## Remote configuration (waypipe)

You only need the GUI for setup, so on a Steam Deck you can configure Keymasq
without leaving Game Mode. The AppImage bundles
[`waypipe`](https://gitlab.freedesktop.org/mstoeckl/waypipe), so you do not
have to install anything on the Deck. On your workstation, install a compatible
`waypipe` from the 0.10+ Rust series, ideally the same version the AppImage
bundles. Then forward the GUI over SSH:

```bash
waypipe -n --remote-bin /opt/keymasq/bin/waypipe ssh deck@<deck-ip> \
  /opt/keymasq/bin/keymasq
```

The GUI runs on the Deck and connects to the `keymasq-session` running there,
but it renders on your workstation's Wayland display. `waypipe` gives the GUI
its own Wayland display, so the GUI never goes through gamescope and the Deck
stays in Game Mode. Your workstation needs a Wayland compositor for this.

## Remote configuration in a browser

The AppImage bundles gtk-brotway, which lets you use the GTK4 GUI from a web
browser. The Brotway endpoint has no authentication, so connect through an SSH
tunnel:

```bash
ssh -L 18101:127.0.0.1:18101 deck@<deck-ip> \
  /opt/keymasq/bin/gtk4-brotway-run --port 18101 /opt/keymasq/bin/keymasq
```

Then open `http://127.0.0.1:18101/` on your workstation. By default the
AppImage launcher binds Brotway to `127.0.0.1` only. On a trusted private
network, you can pass `--address 0.0.0.0` to reach it without a tunnel. Do not
expose that listener to an untrusted network. The launcher also forces the GUI
onto the selected Brotway display, so a `DISPLAY` variable inherited from SSH
X11 forwarding cannot send the GUI somewhere else.

## Updates

The self-update runs through polkit and asks for your password, as the
installer does:

```bash
keymasq --self-update
```

The update verifier uses the host `gpg` command, which SteamOS provides.

The updater runs these steps in order:

1. It downloads a JSON manifest and a detached signature from the Keymasq
   repository.
2. It verifies the manifest with the public key installed under
   `/opt/keymasq/share/keymasq/appimage-update.gpg.asc`.
3. It checks that the manifest architecture matches the running system.
4. It downloads the AppImage the manifest references and checks its SHA-256.
5. It extracts the AppImage into `/opt/keymasq/runtime/<sha256>`.
6. It atomically replaces `/opt/keymasq/Keymasq.AppImage` and repoints
   `/opt/keymasq/runtime/current`.
7. It refreshes the installed host integration files and restarts the
   services.

If the updater cannot restart either systemd service, it exits with an error.
The error says that the files were installed but the services still need to be
recovered. The updater does not report such an update as successful.

The updater compares the manifest version with the installed Keymasq version
and refuses older versions. An attacker who replays an old signed manifest
therefore cannot downgrade your install. To roll back on purpose, use:

```bash
keymasq --self-update --allow-downgrade
```

The update public key embedded in the AppImage is the same key that signs the
package repository. It is published at
`https://repo.keymasq.tools/gpg-key.asc`.

## Uninstall

```bash
keymasq --uninstall
```

The uninstaller removes the AppImage integration, systemd units, udev rules,
Keymasq-managed wrappers, desktop files, and the SteamOS keep-list. It keeps
any commands of your own that share a name with a Keymasq wrapper. After it
removes the hidden-source flags and Keymasq ACL entries, it retriggers the
connected input devices, so you do not need to reboot or replug anything. It
leaves `/etc/keymasq`, `/var/lib/keymasq`, and your configuration and macros in
place on purpose.
