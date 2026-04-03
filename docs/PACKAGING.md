# Packaging

This document explains which installable packages Keyforge provides, what each
package contains, and how those packages are built and tested in this
repository.

If you are looking for end-user installation steps, see `docs/INSTALL.md`.
This file is for someone inspecting the project itself and trying to understand
how packaging is organized.

## Overview

Keyforge is packaged as a single application that installs the same core pieces
on every supported distribution:

- `keyforged`: the privileged system daemon that reads input devices and applies
  remaps
- `keyforge-session`: the per-user session service that tracks desktop/session
  state and talks to the daemon
- `keyforge`: the main CLI and GTK application
- `keyforge-record`: the helper used for privileged recording operations

The repository currently maintains these package outputs:

| Package format | Audience | Source in repo | Build output |
|--------|--------|--------|--------|
| Nix package | Nix and NixOS users | `flake.nix` | `nix build .#default` |
| Nix app | Nix users who want to launch the GUI directly | `flake.nix` | `nix run .#default` |
| NixOS module | NixOS systems | `flake.nix` | `nixosModules.default` |
| Arch local checkout package | Arch users testing/installing the current worktree | `PKGBUILD`, `keyforge.install` | `.pkg.tar.zst` |
| Arch AUR package | AUR publication and release packaging | `packaging/aur/` | AUR Git repo contents |
| Debian package | Debian, Ubuntu, Mint, and derivatives | `debian/` | `.deb` |
| Fedora RPM | Fedora systems | `nfpm.yaml` | `.fedora.x86_64.rpm` |
| openSUSE RPM | openSUSE systems | `nfpm.yaml` | `.opensuse.x86_64.rpm` |

## Shared package payload

Most package formats reuse the same top-level asset directories. The packaging
metadata changes by distribution, but the installed payload is intentionally
kept as similar as possible.

These directories provide the shared package contents:

- `systemd/`: system and user service units
- `udev/`: device access rules
- `sysusers.d/`: creation of the `keyforge` system user/group
- `tmpfiles.d/`: runtime and state directory creation
- `polkit/`: privileged recording policy
- `assets/`: desktop file, AppStream metainfo, and SVG/PNG application icons
- `examples/`: sample configuration files
- `gnome-extension/`: optional GNOME bridge extension

In practice, the distro packages all install the same user-visible pieces:

- executable commands in `/usr/bin/`
- a system service for `keyforged`
- a user service for `keyforge-session`
- udev, sysusers, tmpfiles, and polkit integration
- the desktop launcher and icon
- the AppStream metainfo file
- the GNOME Shell bridge extension files
- `/etc/keyforge/security.toml` as the default security configuration

The Nix outputs split that payload slightly differently:

- the plain Nix package installs the application commands, desktop assets, and
  polkit policy into the Nix store
- the NixOS module wires up the system daemon, user session service, udev ACLs,
  tmpfiles, the `keyforge` system user/group, and `/etc/keyforge/security.toml`
- the plain Nix package does not mutate `/etc`, systemd, or udev on its own

The main filesystem layout is:

```text
/usr/bin/keyforge
/usr/bin/keyforged
/usr/bin/keyforge-session
/usr/bin/keyforge-record
/usr/lib/systemd/system/keyforged.service
/usr/lib/systemd/user/keyforge-session.service
/usr/lib/sysusers.d/keyforge.conf
/usr/lib/tmpfiles.d/keyforge.conf
/usr/lib/udev/rules.d/91-keyforge-acl.rules
/usr/share/polkit-1/actions/com.keyforge.record-macro.policy
/usr/share/applications/keyforge.desktop
/usr/share/metainfo/keyforge.metainfo.xml
/usr/share/icons/hicolor/scalable/apps/keyforge.svg
/usr/share/icons/hicolor/<size>x<size>/apps/keyforge.png
/usr/share/gnome-shell/extensions/keyforge-bridge@keyforge/
/etc/keyforge/security.toml
```

The Python module path differs by package family:

- Debian packages install into `/usr/lib/python3/dist-packages/keyforge/`
- RPM packages install into the target distro `site-packages` path
- Arch packages follow Arch's Python package layout
- Nix packages install into the Nix store

## Available package definitions

### Nix package and NixOS module

`flake.nix` exposes two important outputs:

- `packages.<system>.default`: a build of the Keyforge package itself
- `apps.<system>.default`: a runnable app target for `nix run`
- `nixosModules.default`: a NixOS module that installs the package and wires up
  the system daemon, user session service, udev access, tmpfiles, and the
  generated `/etc/keyforge/security.toml`

This is the most self-contained packaging path in the repository. It is useful
both for Nix users and for developers who want a reproducible build shell.

The flake currently exports Linux builds for:

- `x86_64-linux`
- `aarch64-linux`

Build the package with:

```bash
nix build .#default
```

Run the GUI directly with:

```bash
nix run .#default
```

This app output is mainly useful for package-level smoke testing or temporary
launches. It does not provision the full Keyforge runtime on NixOS by itself:
the daemon, session service, udev ACLs, tmpfiles, system user/group, and
generated `/etc/keyforge/security.toml` are all provided by the NixOS module,
not by `nix run`.

Open the development shell with:

```bash
nix develop
```

The dev shell provides the repo's standard quality tools, including `pytest`,
`ruff`, and `basedpyright`.

For NixOS, the intended consumption path is the module. It exposes:

- `services.keyforge.enable`
- `services.keyforge.package`
- `services.keyforge.installPackage`
- `services.keyforge.securityConfig`

Functional module usage:

```nix
{
  inputs.keyforge.url = "github:nyrda/keyforge";

  outputs = { self, nixpkgs, keyforge, ... }: {
    nixosConfigurations.my-host = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        keyforge.nixosModules.default
        ({ pkgs, ... }: {
          services.keyforge = {
            enable = true;
            installPackage = true;
          };
        })
      ];
    };
  };
}
```

### Arch Linux

Pacman packaging is generated from shared templates under
`packaging/pacman/templates/` by:

```bash
python3 packaging/pacman/render.py
```

This produces two Arch-facing outputs with the same install payload:

- `PKGBUILD` and `keyforge.install` at the repo root for local worktree
  installs
- `packaging/aur/PKGBUILD`, `packaging/aur/keyforge.install`, and
  `packaging/aur/.SRCINFO` for the AUR package repo

The root `PKGBUILD` is for `git clone ... && makepkg -sif` testing. It copies
the current checkout into `$srcdir`, builds a wheel from that local snapshot,
and does not fetch an external archive or VCS source.

The `packaging/aur/` subtree is for release publishing. It expects a tagged
release tarball URL and checksum. The GitHub release workflow is the intended
path for updating and pushing that subtree to the AUR repo.

The workflow also builds the rendered Arch package from the release tarball and
installs that exact artifact in a fresh Arch environment for smoke testing.
Real AUR publication is gated behind the repository variable
`ENABLE_AUR_PUBLISH=true`.

Both variants build a Python wheel, install the shared service and integration
files, and produce the same pacman package payload.

Local build from the current checkout:

```bash
makepkg -sif
```

### Debian and Ubuntu

Debian packaging is native to Debian tooling and lives under `debian/`.

Important files include:

- `debian/control`: package metadata and runtime dependencies
- `debian/rules`: build rules
- `debian/keyforge.install`: install manifest for the shared payload
- `debian/tests/`: installed-package smoke tests and CLI tests

The Debian package builds the Python package with `pybuild`, stages the shared
payload, and preserves `/etc/keyforge/security.toml` as a Debian conffile so
local edits survive upgrades.

Build the binary package with:

```bash
dpkg-buildpackage -us -uc -b
```

Typical output:

```text
../keyforge_0.1.0-1_all.deb
```

Inspect the resulting package with:

```bash
dpkg-deb -I ../keyforge_0.1.0-1_all.deb
dpkg-deb -c ../keyforge_0.1.0-1_all.deb
```

### Fedora and openSUSE RPMs

RPM packaging is driven by `nfpm.yaml`. The repository uses a single shared
definition and applies distro-specific dependency overrides when generating the
Fedora and openSUSE variants.

This packaging path exists because the payload is mostly identical across the
RPM-based targets, while dependency names and Python library paths differ.

The RPM build flow:

1. Builds a Python wheel
2. Installs that wheel into a staging directory
3. Resolves the target distro's Python `site-packages` path
4. Runs `nfpm` with distro-specific dependency values

Build RPMs with:

```bash
FEDORA_BUILD_HOST=<fedora-builder> \
OPENSUSE_BUILD_HOST=<opensuse-builder> \
bash scripts/build-packages.sh
```

Typical output:

```text
dist/keyforge-0.1.0-1.fedora.x86_64.rpm
dist/keyforge-0.1.0-1.opensuse.x86_64.rpm
```

If you only provide one compatible build host, the script builds only that RPM
variant.

## Build environments

For general repository work, use the Nix dev shell:

```bash
nix develop
```

That shell covers source checks, but native distro packaging still requires the
relevant distro tooling.

For Debian package work on Debian or Ubuntu, install:

```bash
sudo apt-get install -y \
  autopkgtest \
  debhelper \
  dh-sequence-python3 \
  lintian \
  pybuild-plugin-pyproject \
  python3-all \
  python3-build \
  python3-installer \
  python3-setuptools \
  python3-wheel \
  qemu-system-x86 \
  qemu-utils \
  virtinst
```

RPM builds additionally require access to a Fedora or openSUSE environment with
the right Python and RPM metadata available. In this repository that is usually
handled through `scripts/build-packages.sh`.

## Testing and verification

Package validation happens at two levels: source checks and installed-package
checks.

Run the standard source checks first:

```bash
./scripts/check.sh
```

This wrapper runs `ruff` and `basedpyright` in the Nix dev shell, then runs the
full pytest suite in the Nix VM harness because the runtime tests need `uinput`
and other integration pieces that are not reliable on the host shell alone.

### Debian package tests

The Debian package defines two `autopkgtest` tests in `debian/tests/`:

- `pkg-smoke`: validates installed files, services, sockets, and basic runtime
  behavior
- `installed-cli`: runs a curated pytest subset against the installed package

Typical Debian verification flow:

```bash
dpkg-buildpackage -us -uc -b
lintian ../keyforge_0.1.0-1_all.deb
autopkgtest . -- qemu /path/to/debian-autopkgtest.qcow2
```

For libvirt-backed local testing, the repository also provides:

- `packaging/debian/create-libvirt-autopkgtest-vm.sh`
- `packaging/debian/run-autopkgtest-libvirt.sh`

If you need to debug the package inside a guest, install the built `.deb` and
run the test scripts directly:

```bash
sudo apt-get install -y ../keyforge_0.1.0-1_all.deb
sudo sh debian/tests/pkg-smoke
sh debian/tests/installed-cli
```

### Cross-distro VM tests

For broader package validation across Debian-like and RPM-based systems, the
repository includes VM-oriented helper scripts:

- `scripts/build-packages.sh`: builds `.deb` and RPM artifacts
- `scripts/test-vms.sh`: copies a package into test VMs, installs it, enables
  services, runs package validation, and can also run pytest remotely
- `scripts/vm-package-checks.sh`: the shared validation script used inside those
  VMs

This is mainly a maintainer workflow, but it is useful if you want to confirm
that packages behave the same way across multiple distributions.

## CI and release artifacts

The package workflow lives in `.github/workflows/package.yml`.

At a high level, CI:

1. Runs source checks
2. Builds the rendered Arch package from the release tarball
3. Installs and smoke-tests that Arch package in a clean Arch environment
4. Builds the Debian package
5. Tests the exact built `.deb` in a clean Debian environment
6. Builds Fedora and openSUSE RPMs in distro-native environments
7. Installs and smoke-tests those RPMs in fresh environments
8. Publishes build artifacts on release tags

On tagged builds, the workflow publishes:

- `.deb`
- `.changes`
- `.buildinfo`
- Fedora RPM
- openSUSE RPM

You can exercise parts of the workflow locally with `act`:

```bash
act -j build-deb -W .github/workflows/package.yml
act -j test-deb -W .github/workflows/package.yml
act -j build-rpm-fedora -W .github/workflows/package.yml
act -j test-rpm-fedora -W .github/workflows/package.yml
```

## Known packaging notes

- Fedora and openSUSE RPMs are generated from the same `nfpm.yaml`, but they
  still need distro-specific dependency names and Python paths.
- The GNOME bridge extension files are installed by the package, but GNOME users
  must enable the extension explicitly after installation. Packages install the
  files; they do not enable the GNOME Shell extension on the user's behalf.
- Debian packaging is native and repo-local under `debian/`, while RPM
  packaging stays on the `nfpm` flow. This split is intentional.
