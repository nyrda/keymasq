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
with `pacman`, then downloads the pinned AnyLinux `quick-sharun` helper. The
build also downloads checksum-pinned `sharun`, `appimagetool`, `uruntime`,
`mkdwarfs`, and `anylinux.c` inputs up front; the helper is prevented from
fetching moving, unverified, or optional inputs during the build.

```bash
bash packaging/appimage/get-dependencies.sh
bash packaging/appimage/make-appimage.sh
```

The output is written to `dist/appimage/`.

### Updating pinned build helpers

Review the helper pins before every stable Keymasq release. If releases are
farther apart, review them at least quarterly, and update immediately for an
upstream security advisory or a toolchain compatibility break.

Keep each version/revision and its checksums in
`packaging/appimage/make-appimage.sh` in one change:

- `ANYLINUX_REV`, `QUICK_SHARUN_SHA256`, and `ANYLINUX_SOURCE_SHA256`
- `SHARUN_VERSION` and the x86_64/aarch64 `SHARUN_SHA256` values
- `APPIMAGETOOL_VERSION` and the x86_64/aarch64 `APPIMAGETOOL_SHA256` values
- `URUNTIME_VERSION` and the x86_64/aarch64 `URUNTIME_SHA256` values
- `DWARFS_VERSION` and the x86_64/aarch64 `DWARFS_SHA256` values

Resolve exact upstream tags/commits, never moving `main` or `latest` URLs, and
calculate every value from the downloaded bytes:

```bash
curl -fsSL <exact-url> | sha256sum
```

The matching `KEYMASQ_APPIMAGE_*` URL, version, revision, and checksum
environment variables can be used to trial a complete candidate pin set
without editing the script. A checksum override must accompany every URL or
version override. After updating, run the complete build and extracted-runtime
verification in a clean Arch environment:

```bash
bash packaging/appimage/get-dependencies.sh
bash packaging/appimage/make-appimage.sh
bash packaging/appimage/verify-appimage.sh "$(echo dist/appimage/Keymasq-*.AppImage)"
```

Review the helper diff for new network fetches whenever `ANYLINUX_REV` changes;
the build deliberately disables optional hooks, GTK class fixes, and static
launcher/path-mapping optimization so those paths cannot introduce unverified
downloads.

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
/etc/polkit-1/rules.d/50-keymasq-record.rules
/etc/keymasq/security.toml
/etc/profile.d/keymasq.sh
/etc/atomic-update.conf.d/keymasq.conf
```

plus the desktop entry and the `keymasq-session` user unit under the target
user's home directory. The target user is the invoking desktop user;
`--install --user USER` overrides it. `/etc/profile.d/keymasq.sh` puts
`/opt/keymasq/bin` on the login `PATH`. On non-SteamOS systems with a writable
polkit action directory, the installer also writes the matching
`/usr/share/polkit-1/actions/com.keymasq.record-macro.policy`.

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

Self-update extracts the new runtime before switching `/opt/keymasq/runtime/current`
and refreshes the installed wrappers, rules, service files, desktop files, and
update public key from that extracted runtime.
