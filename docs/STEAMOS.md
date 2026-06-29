# SteamOS / Steam Deck (AppImage)

Keymasq ships an AnyLinux AppImage for SteamOS and other distributions without
a native package. It bundles a self-contained x86_64
runtime and installs a small host integration layer for systemd, udev,
sysusers, and SteamOS update persistence.

## Install

On a Steam Deck, switch to Desktop Mode and run these commands from a terminal
(Konsole). Download `Keymasq-*-x86_64.AppImage` from the
[GitHub releases](https://github.com/nyrda/keymasq/releases), make it
executable, and run the installer:

```bash
chmod +x Keymasq-*-x86_64.AppImage
./Keymasq-*-x86_64.AppImage --install
```

The installer needs root and prompts through polkit. It installs user-facing
wrappers and desktop files for the invoking user by default. To target a
different desktop user, pass `--user USER`.

The installer enables and starts both services for you (`keymasqd` system-wide
and `keymasq-session` for the target user), so no extra `systemctl` step is
needed. Launch the GUI when it finishes:

```bash
keymasq
```

The rest of this page documents what the installer puts on disk and how
updates, persistence, and uninstall behave.

## Install Model

The AppImage installs itself to:

```text
/opt/keymasq/Keymasq.AppImage
/opt/keymasq/runtime/<sha256>/
/opt/keymasq/runtime/current -> <sha256>
/opt/keymasq/bin/keymasq
/opt/keymasq/bin/keymasqd
/opt/keymasq/bin/keymasq-session
/opt/keymasq/bin/keymasq-record
/opt/keymasq/bin/waypipe
~/.local/bin/keymasq
~/.local/bin/keymasqd
~/.local/bin/keymasq-session
~/.local/bin/keymasq-record
~/.local/bin/waypipe
```

System integration is installed under `/etc`:

```text
/etc/sysusers.d/keymasq.conf
/etc/tmpfiles.d/keymasq.conf
/etc/systemd/system/keymasqd.service
/etc/udev/rules.d/91-keymasq-acl.rules
/etc/udev/rules.d/99-keymasq-hide-grabbed.rules
/etc/keymasq/security.toml
/etc/profile.d/keymasq.sh
/etc/atomic-update.conf.d/keymasq.conf
```

The per-user service and desktop entry are installed under the target user's
home directory.

`Keymasq.AppImage` remains the signed update payload. The installer extracts it
once into `/opt/keymasq/runtime/<sha256>` and the wrappers run commands from
`/opt/keymasq/runtime/current`. Daemon, session, and GUI starts therefore reuse
the same extracted runtime instead of extracting the AppImage on every start.

On install, the AppImage runs `systemd-sysusers` and `systemd-tmpfiles --create`
once itself. After SteamOS updates, the preserved sysusers and tmpfiles
configuration lets the normal boot-time systemd units recreate the `keymasq`
user and directories before `keymasqd.service` starts. `keymasqd.service` also
uses `RuntimeDirectory=` and `StateDirectory=`, so systemd creates
`/run/keymasq` and `/var/lib/keymasq` when the daemon starts.

## SteamOS Persistence

On SteamOS, `/opt` is a bind mount from the offload area on the home/var data
partition (`/home/.steamos/offload/opt`), not the read-only A/B rootfs
(verified on SteamOS 3.8: `findmnt -T /opt` reports the `/home` partition).
The AppImage payload and extracted runtime live in `/opt/keymasq`, so they
persist across atomic OS updates on their own and need no keep-list entry. The
atomic-update allow list only governs `/etc`, so the keep-list only needs the
third-party `/etc` integration files.

The installer writes `/etc/atomic-update.conf.d/keymasq.conf` with:

```text
/etc/atomic-update.conf.d/keymasq.conf
/etc/keymasq/**
/etc/profile.d/keymasq.sh
/etc/sysusers.d/keymasq.conf
/etc/tmpfiles.d/keymasq.conf
/etc/systemd/system/keymasqd.service
/etc/udev/rules.d/91-keymasq-acl.rules
/etc/udev/rules.d/99-keymasq-hide-grabbed.rules
```

## Security Defaults

SteamOS/AppImage installs default to:

```toml
[recording_guard]
unlock_required = false
macro_edit_requires_unlock = false
```

Existing `/etc/keymasq/security.toml` files are not overwritten.

## Remote Configuration (waypipe)

The GUI is only needed for setup, not at runtime, so on a Steam Deck you do not
have to leave Game Mode to configure Keymasq. The AppImage bundles
[`waypipe`](https://gitlab.freedesktop.org/mstoeckl/waypipe), so the Deck side
needs nothing extra — install a compatible `waypipe` on your workstation
(ideally the same version; the 0.10+ Rust series), then forward the GUI over
SSH:

```bash
waypipe --remote-bin /opt/keymasq/bin/waypipe ssh deck@<deck-ip> \
  /opt/keymasq/bin/keymasq
```

The GUI runs on the Deck and connects to the running `keymasq-session` there,
while it renders on your workstation's Wayland display. gamescope is not
involved: `waypipe` gives the GUI its own Wayland display, so the Deck keeps
running Game Mode untouched. This needs a Wayland compositor on your
workstation. If GPU buffer forwarding misbehaves across machines, fall back to
software rendering with `waypipe -n` and `GSK_RENDERER=cairo`.

## Updates

Self-updates run through polkit:

```bash
keymasq --self-update
```

The update verifier uses the host `gpg` command, which SteamOS provides.

The updater downloads a JSON manifest and detached signature from the Keymasq
repository, verifies the manifest with the public key installed under
`/opt/keymasq/share/keymasq/appimage-update.gpg.asc`, downloads the referenced
AppImage, checks its SHA-256, extracts it into
`/opt/keymasq/runtime/<sha256>`, atomically replaces
`/opt/keymasq/Keymasq.AppImage`, atomically repoints
`/opt/keymasq/runtime/current`, and restarts services.

Signed manifest replays cannot downgrade an installed build by default: the
updater compares the manifest version with the installed Keymasq version and
refuses older versions. For an intentional rollback, use:

```bash
keymasq --self-update --allow-downgrade
```

The embedded update public key is the repository/package signing public key
published at `https://repo.keymasq.tools/gpg-key.asc`.

Published manifests are signed by the GitHub Actions repository publish job; the
private signing key is not needed locally. For local VM testing before GitHub
Actions signs a manifest, use:

```bash
keymasq --self-update --allow-unsigned
```

This still checks the AppImage SHA-256 from the manifest. Do not use unsigned
updates for published channels.

## Uninstall

```bash
keymasq --uninstall
```

Uninstall removes AppImage integration, systemd units, udev rules, wrappers,
desktop files, and the SteamOS keep-list. It intentionally leaves
`/etc/keymasq`, `/var/lib/keymasq`, and user configuration/macros in place.
