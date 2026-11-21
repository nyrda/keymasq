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

Then make the download executable and open it. In the Dolphin file manager,
right-click the file, choose **Properties**, and mark it as executable on the
**Permissions** tab. Then double-click the file.

Keymasq starts and offers to install itself:

1. Click **Install** and enter your password.
2. Keymasq copies itself to `/opt/keymasq`, starts the background services, and
   adds itself to the application menu. The window shows each step.
3. Click **Done**. The window connects to the services and is ready to use.

From then on, launch Keymasq from the application menu. You can delete the
downloaded file.

If you prefer a terminal, the same installer runs from the command line:

```bash
cd ~/Downloads
chmod +x Keymasq-*-x86_64.AppImage
./Keymasq-*-x86_64.AppImage --install
```

Then start the GUI from the application menu, or from the same terminal:

```bash
/opt/keymasq/bin/keymasq
```

The plain `keymasq` command works in terminals opened after your next login.

The window starts the same installer you can run in a terminal. If it cannot
show a password prompt, it tells you which command to run instead. This
happens in Game Mode and in remote sessions, because they have no desktop
password prompt.

After setup, you can go back to Game Mode. Remapping runs as a background
service and does not need the GUI or Desktop Mode.

The rest of this page covers what the installer puts on disk, how the install
persists across SteamOS updates, and how to update and uninstall Keymasq.

## Install model

The installer writes to two system locations:

- `/opt/keymasq` holds the application. That is the AppImage, the extracted
  runtime it runs from, and the commands under `/opt/keymasq/bin`.
- `/etc` holds the system integration. That is the `keymasqd` service, udev
  rules, the hardware job unit and its polkit rule, `/etc/keymasq/security.toml`,
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

On GNOME, the installer also copies the Keymasq GNOME Shell extension to
`~/.local/share/gnome-shell/extensions/`. The GUI then guides you through
enabling it, as described in [gnome.md](gnome.md).

## SteamOS persistence

A SteamOS update replaces the OS but leaves Keymasq in place. You do not need
to reinstall or re-run anything afterwards.

On SteamOS, `/opt` is on the same persistent partition as `/home`. OS updates
leave that partition alone, so the runtime under `/opt/keymasq` and your
configuration in `/home` both survive them. SteamOS does delete third-party
files from `/etc` during updates. To prevent that, the installer lists every
file it put there in a keep-list at `/etc/atomic-update.conf.d/keymasq.conf`.

## Security defaults

SteamOS/AppImage installs use the same `/etc/keymasq/security.toml` defaults
as native packages. Macro recording is allowed and stops after 10 minutes:

```toml
[recording_guard]
macro_recording_allowed = true
macro_recording_time_limit = 10
```

See [Security model](security.md#policy-file) for the other settings.

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

## Install & Updates in the GUI

The main menu of an AppImage install has an **Install & Updates** entry. It
shows the installed version and whether the background services run, and it
offers these actions:

- **Check Now** looks for a new release. **Update** installs it and asks for
  your password. Afterwards, click **Restart Keymasq** to switch the window to
  the new version.
- **Check automatically** controls the update check that runs when Keymasq
  starts, at most once a day. The check only downloads the signed release
  manifest from `repo.keymasq.tools`. Keymasq never installs an update
  without asking.
- **Repair** writes the services, device rules, and commands again and
  restarts the services. Use it when a service does not start.
- **Uninstall** removes Keymasq, as described under [Uninstall](#uninstall).

When an update is available, the main window also shows a banner with an
**Update** button.

### Opening a downloaded AppImage while Keymasq is installed

The window and the background services have to run the same version. If you
open a downloaded AppImage whose version differs from the installed one,
Keymasq asks what to do:

- A newer AppImage offers to update the installation to its version.
- An older AppImage offers to open the installed version instead. Downgrading
  to the AppImage's version is the second choice.

### Other users on the same system

The installer sets up the session service for the user who ran it. When
another user opens Keymasq, it offers to set up their account. This does not
need a password. The terminal equivalent is:

```bash
/opt/keymasq/bin/keymasq --setup-user
```

## Updates

The self-update downloads and checks the new release as your user. It asks
for your password through polkit only when a verified update is ready to
install:

```bash
keymasq --self-update
```

If the newest release is already installed, or the download or a check fails,
the updater stops before the password prompt.

To only check for a new release, without a password:

```bash
keymasq --check-update
```

The update verifier uses the host `gpg` command, which SteamOS provides.

The updater runs these steps in order. Steps 1 to 4 run as your user and
steps 5 to 8 run as root:

1. It downloads a JSON manifest and a detached signature from the Keymasq
   repository.
2. It verifies the manifest with the public key installed under
   `/opt/keymasq/share/keymasq/appimage-update.gpg.asc`.
3. It checks that the manifest architecture matches the running system and
   that the release is not older than the installed one.
4. It downloads the AppImage the manifest references and checks its SHA-256.
5. After the password prompt, the installed release takes its own copies of
   the manifest, the signature, and the AppImage and repeats the signature,
   architecture, version, and SHA-256 checks on them. Root trusts none of the
   checks from steps 2 to 4, so a file changed after the download is rejected.
6. It extracts the AppImage into a staging directory, checks that the copy
   finished and that every file in the AppImage's runtime file list is present
   with its expected size, then renames it to `/opt/keymasq/runtime/<sha256>`. A
   failed copy, for example on a full disk, removes the staging directory.
   An incomplete `/opt/keymasq/runtime/<sha256>` left by an earlier attempt is
   replaced instead of reused.
7. It atomically replaces `/opt/keymasq/Keymasq.AppImage` and repoints
   `/opt/keymasq/runtime/current`.
8. It refreshes the installed host integration files and restarts the
   services.

Running `sudo keymasq --self-update` performs all steps as root.

If the updater cannot restart either systemd service, it exits with an error.
The error says that the files were installed but the services still need to be
recovered. The updater does not report such an update as successful.

The installed release runs the update, so updates started from v0.20.0 or
older still reuse an incomplete runtime directory. If such an update keeps
failing with `updated runtime missing AppImage integration assets` after the
disk ran full, free some space and run `--install` from the new AppImage once.
It replaces the incomplete runtime.

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

Choose **Install & Updates** in the main menu, then **Uninstall**. Or run:

```bash
keymasq --uninstall
```

The uninstaller removes the AppImage integration, systemd units, udev rules,
Keymasq-managed wrappers, desktop files, the GNOME Shell extension it
installed, and the SteamOS keep-list. It keeps
any commands of your own that share a name with a Keymasq wrapper. After it
removes the hidden-source flags and Keymasq ACL entries, it retriggers the
connected input devices, so you do not need to reboot or replug anything. It
leaves `/etc/keymasq`, `/var/lib/keymasq`, and your configuration and macros in
place on purpose.

Before it removes anything, the uninstaller stops `keymasqd` and restores masked
devices. If that recovery cannot finish, it stops with an error and leaves
Keymasq installed. See [Uninstalling](hardware-masking.md#uninstalling).
