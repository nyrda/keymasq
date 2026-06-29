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
