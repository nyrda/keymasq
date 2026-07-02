# Keymasq AnyLinux AppImage Packaging

This README is for maintainers building and publishing the AppImage. User-facing
install, update, uninstall, SteamOS persistence, and service behavior are
documented in `docs/INSTALL.md` and `docs/STEAMOS.md`.

## Scope

The AppImage path builds one x86_64 AnyLinux AppImage. It bundles the Python
runtime, Keymasq, Python dependencies, GTK4/libadwaita, GObject introspection
data, the `slurp` point-pick helper, and the `waypipe` helper for forwarding
the GUI to a remote workstation. Host integration is installed by the AppImage
at runtime.

SteamOS is the primary target. The installer uses systemd when available and
falls back to a core install plus manual daemon-supervisor instructions on
non-systemd systems.

## Build

The build scripts are intended to run in an Arch environment. The GitHub
Actions package workflow uses `archlinux:base-devel`, installs dependencies
with `pacman`, then downloads the pinned AnyLinux `quick-sharun` helper.

```bash
bash packaging/appimage/get-dependencies.sh
bash packaging/appimage/make-appimage.sh
```

The output is written to `dist/appimage/`.

## Graphics Policy

Default builds leave GTK rendering on `auto` and do not bundle Mesa, GLVND,
OpenGL, Vulkan, GBM, DRM, or DRI driver files. Real SteamOS hardware uses the
host graphics stack through GTK's normal GL/Vulkan-capable path.

For a special VM-only software-rendering artifact, set:

```bash
KEYMASQ_APPIMAGE_ALWAYS_SOFTWARE=1 bash packaging/appimage/make-appimage.sh
```

For runtime smoke tests in a VM, prefer the narrower runtime override instead:

```bash
KEYMASQ_APPIMAGE_RENDERING=software keymasq
```

## Installed Layout

`--install` writes:

```text
/opt/keymasq/Keymasq.AppImage
/opt/keymasq/runtime/<sha256>/
/opt/keymasq/runtime/current -> <sha256>
/opt/keymasq/bin/{keymasq,keymasqd,keymasq-session,keymasq-record,waypipe}
~/.local/bin/{keymasq,keymasqd,keymasq-session,keymasq-record,waypipe}
/etc/sysusers.d/keymasq.conf
/etc/tmpfiles.d/keymasq.conf
/etc/systemd/system/keymasqd.service
/etc/udev/rules.d/91-keymasq-acl.rules
/etc/udev/rules.d/99-keymasq-hide-grabbed.rules
/etc/keymasq/security.toml
/etc/profile.d/keymasq.sh
/etc/atomic-update.conf.d/keymasq.conf
```

plus the desktop entry and the `keymasq-session` user unit under the target
user's home directory. The target user is the invoking desktop user;
`--install --user USER` overrides it. `/etc/profile.d/keymasq.sh` puts
`/opt/keymasq/bin` on the login `PATH`.

`Keymasq.AppImage` remains the signed update payload. The installer extracts
it once into `/opt/keymasq/runtime/<sha256>` and the wrappers run commands
from `/opt/keymasq/runtime/current`, so daemon, session, and GUI starts reuse
the same extracted runtime instead of extracting the AppImage on every start.

The installer runs `systemd-sysusers` and `systemd-tmpfiles --create` once
itself. After SteamOS updates, the preserved sysusers and tmpfiles
configuration lets the normal boot-time systemd units recreate the `keymasq`
user and directories before `keymasqd.service` starts. `keymasqd.service` also
uses `RuntimeDirectory=` and `StateDirectory=`, so systemd creates
`/run/keymasq` and `/var/lib/keymasq` when the daemon starts.

## SteamOS Persistence Notes

On SteamOS, `/opt` is a bind mount from the offload area on the home/var data
partition (`/home/.steamos/offload/opt`), not the read-only A/B rootfs;
verified on SteamOS 3.8, where `findmnt -T /opt` reports the `/home`
partition. The AppImage payload and extracted runtime under `/opt/keymasq`
therefore persist across atomic OS updates without a keep-list entry. The
atomic-update allow list only governs `/etc`, which is why the installer's
keep-list at `/etc/atomic-update.conf.d/keymasq.conf` contains only the `/etc`
integration files.

## Update Manifest

The updater downloads a signed JSON manifest from:

```text
https://repo.keymasq.tools/appimage/latest-x86_64.json
```

Manifest shape:

```json
{
  "version": "0.18.0",
  "architecture": "x86_64",
  "appimage_url": "https://repo.keymasq.tools/appimage/Keymasq-0.18.0-x86_64.AppImage",
  "sha256": "..."
}
```

Published manifests are signed by the GitHub Actions repository publish job
with the existing package signing key. Developers do not need the private key
locally. The signing step is:

```bash
gpg --batch --yes --detach-sign --armor latest-x86_64.json
```

For local VM testing before GitHub Actions signs a manifest, skip the signature
check explicitly:

```bash
keymasq --self-update --allow-unsigned
```

This still checks the AppImage SHA-256 from the manifest. Do not use unsigned
updates for published channels.
