# Packaging

This document explains which installable packages Keymasq provides, what each
package contains, and how those packages are built and tested in this
repository.

If you are looking for end-user installation steps, see `docs/INSTALL.md`.
This file is for someone inspecting the project itself and trying to understand
how packaging is organized.

## Overview

Keymasq is packaged as a single application that installs the same core pieces
on every supported distribution:

- `keymasqd`: the privileged system daemon that reads input devices and applies
  remaps
- `keymasq-session`: the per-user session service that tracks desktop/session
  state and talks to the daemon
- `keymasq`: the main CLI and GTK application
- `keymasq-record`: the helper used for privileged recording operations

The repository currently maintains these package outputs:

| Package format | Audience | Source in repo | Build output |
|--------|--------|--------|--------|
| Nix package | Nix and NixOS users | `flake.nix` | `nix build .#default` |
| NixOS module | NixOS systems | `flake.nix` | `nixosModules.default` |
| Arch local checkout package | Arch users testing/installing the current worktree | `PKGBUILD`, `keymasq.install` | `.pkg.tar.zst` |
| Arch AUR package | AUR publication and release packaging | `packaging/aur/` | AUR Git repo contents |
| Debian package | Debian, Ubuntu, Mint, and derivatives | `debian/` | `.deb` |
| Fedora COPR | Fedora systems | `packaging/rpm/` | COPR-hosted RPM repository |
| Fedora project-hosted RPM | Fedora systems | `packaging/rpm/` | `.fc<release>.noarch.rpm` |
| openSUSE RPM | openSUSE systems | `packaging/rpm/` | `.opensuse.x86_64.rpm` |
| AnyLinux AppImage | SteamOS and other systemd distros without a native package | `packaging/appimage/` | `Keymasq-<version>-x86_64.AppImage` |

## Release channels

The repository uses two packaging channels:

- Stable releases are created from stable `v*` tags. These are the only runs
  that sign RPMs, publish GitHub release artifacts as the official release, and
  optionally push AUR and external package repositories.
- Prereleases are created manually with the `Package` workflow's
  `workflow_dispatch` inputs. They build from an explicit ref, upload unsigned
  artifacts to a GitHub prerelease, and do not publish to AUR or external
  repositories.

## Release checklist

Before tagging a stable `v*` release, run the manual VM gates from
[VM_TESTING.md](VM_TESTING.md) in full, regardless of what changed since the
last tag:

- [ ] `./scripts/check.sh full`
- [ ] `./scripts/integration.sh daemon-session`
- [ ] `./scripts/integration.sh listeners`
- [ ] `scripts/check-doc-screenshots`

These suites are manual gates and are not run by CI. Prereleases should meet
the same bar unless the prerelease exists specifically to test packaging
changes.

## Version bump workflow

Use `scripts/release-version.py` as the single entrypoint for release-version
updates:

```bash
python3 scripts/release-version.py 0.3.1
```

The script updates the maintained version surfaces and regenerates the derived
pacman packaging outputs from `packaging/pacman/templates/`. In particular it
updates:

- `pyproject.toml`
- `flake.nix`
- `packaging/rpm/metadata.env`
- `debian/changelog`
- `CHANGELOG.md`
- `assets/tools.keymasq.keymasq.metainfo.xml`
- the generated pacman files: `PKGBUILD`, `keymasq.install`,
  `packaging/aur/PKGBUILD`, `packaging/aur/keymasq.install`, and
  `packaging/aur/.SRCINFO`

Do not edit the rendered pacman files directly. The release helper regenerates
them from `packaging/pacman/render.py`, and the package workflow later reruns
that renderer with the final release tarball checksum before publishing to AUR.

Before running the script for a release, update the top release notes content
that should be preserved in place:

- the top `CHANGELOG.md` section body for the new version if you are writing it
  before the bump
- the top entry bullet list in `debian/changelog`
- the top `<release>` description in `assets/tools.keymasq.keymasq.metainfo.xml`
  if you are writing it before the bump

The script then normalizes or inserts the top `CHANGELOG.md` section for the
requested version, updates the current release date in Debian changelog and
AppStream metadata, and refreshes the generated packaging files. Use
`--dry-run` to preview the file set, or `--release-date YYYY-MM-DD` when a
non-today date is required.

When writing `CHANGELOG.md`, only include user-facing software changes:

- include new features, removed features, user-visible fixes, and meaningful
  changes in runtime behavior
- exclude docs, troubleshooting notes, tests, CI, refactors, release tooling,
  and packaging implementation changes
- do not treat broader test coverage as user-facing
- describe the user-visible outcome rather than the internal implementation

## Shared package payload

Most package formats reuse the same top-level asset directories. The packaging
metadata changes by distribution, but the installed payload is intentionally
kept as similar as possible.

These directories provide the shared package contents:

- `systemd/`: system and user service units
- `udev/`: device access rules
- `sysusers.d/`: creation of the `keymasq` system user/group
- `tmpfiles.d/`: runtime and state directory creation
- `polkit/`: privileged recording policy
- `assets/`: desktop file, AppStream metainfo, and SVG/PNG application icons
- `examples/`: sample configuration files
- `gnome-extension/`: optional GNOME bridge extension

In practice, the distro packages all install the same user-visible pieces:

- executable commands in `/usr/bin/`
- a system service for `keymasqd`
- a user service for `keymasq-session`
- udev, sysusers, tmpfiles, and polkit integration
- the desktop launcher and icon
- the AppStream metainfo file
- the GNOME Shell bridge extension files
- `/etc/keymasq/security.toml` as the default security configuration

## Service restart policy

Packages should not enable or start Keymasq services automatically on first
install. Users should explicitly enable `keymasqd` and `keymasq-session`.

On upgrades, Debian, Arch, Fedora, and openSUSE packages try to restart
`keymasqd` only if it is already running. The NixOS module uses systemd
`restartTriggers` for the same package-change behavior. The packaged
`keymasq-session` user unit is configured to exit after an established
`keymasqd` connection is lost, so systemd's `Restart=on-failure` restarts the
session service without root package scripts needing to manage per-user systemd
instances.

The GUI is never restarted by packaging.

The Nix outputs split that payload slightly differently:

- the plain Nix package installs the application commands, desktop assets,
  source-hiding udev rule files, and polkit policy into the Nix store
- the NixOS module wires up the system daemon, user session service, udev ACLs
  and source-hiding rules, tmpfiles, the `keymasq` system user/group, and
  `/etc/keymasq/security.toml`
- the plain Nix package does not mutate `/etc`, systemd, or udev on its own

The main filesystem layout is:

```text
/usr/bin/keymasq
/usr/bin/keymasqd
/usr/bin/keymasq-session
/usr/bin/keymasq-record
/usr/lib/systemd/system/keymasqd.service
/usr/lib/systemd/user/keymasq-session.service
/usr/lib/sysusers.d/keymasq.conf
/usr/lib/tmpfiles.d/keymasq.conf
/usr/lib/udev/rules.d/91-keymasq-acl.rules
/usr/lib/udev/rules.d/99-keymasq-hide-grabbed.rules
/usr/share/polkit-1/actions/com.keymasq.record-macro.policy
/usr/share/applications/tools.keymasq.keymasq.desktop
/usr/share/metainfo/tools.keymasq.keymasq.metainfo.xml
/usr/share/icons/hicolor/scalable/apps/tools.keymasq.keymasq.svg
/usr/share/icons/hicolor/<size>x<size>/apps/tools.keymasq.keymasq.png
/usr/share/gnome-shell/extensions/gnome-bridge@keymasq.tools/
/etc/keymasq/security.toml
```

The Python module path differs by package family:

- Debian packages install into `/usr/lib/python3/dist-packages/keymasq/`
- RPM packages install into the target distro `site-packages` path
- Arch packages follow Arch's Python package layout
- Nix packages install into the Nix store

`uvloop` is an optional Python speedup declared in the `speedups` extra of
`pyproject.toml`, not a required base dependency. Keymasq uses it as the
default `asyncio` policy for `keymasqd` and `keymasq-session` when available,
and falls back to the stdlib loop with a warning if it is missing or broken.
Most maintained packages still install it by default (hard dependency on
Arch and Debian, bundled in the AppImage and Nix builds, weak dependency on
the RPM targets); see `docs/DEPENDENCIES.md`.

Source-hiding udev rules call `setfacl` from the ACL utilities when hiding a
grabbed physical gamepad source. Source builds and downstream packages must
include the distro package that provides `setfacl` (`acl` on the maintained
Debian, Arch, Fedora, openSUSE, and Nix packaging paths).

## Available package definitions

### AnyLinux AppImage for SteamOS

The AppImage path lives under `packaging/appimage/`. It targets SteamOS first
and currently builds x86_64 only. The AppImage bundles Python, Keymasq, Python
dependencies, GTK4/libadwaita, and GObject introspection data using AnyLinux
`quick-sharun`.

Build inside an Arch environment:

```bash
bash packaging/appimage/get-dependencies.sh
bash packaging/appimage/make-appimage.sh
bash packaging/appimage/verify-appimage.sh "$(echo dist/appimage/Keymasq-*.AppImage)"
```

The output is written to `dist/appimage/`. The GitHub Actions package workflow
does this in `archlinux:base-devel`, installing dependencies with `pacman` and
downloading the pinned AnyLinux `quick-sharun` helper from
`pkgforge-dev/Anylinux-AppImages`. The helper's `sharun`, `appimagetool`, and
`uruntime`, `mkdwarfs`, and `anylinux.c` inputs are separately
version/checksum-pinned and prepared before the helper runs, so release builds
do not consume moving or unverified transitive inputs.

The AppImage installs the signed payload at `/opt/keymasq/Keymasq.AppImage`,
extracts it once into `/opt/keymasq/runtime/<sha256>`, points
`/opt/keymasq/runtime/current` at that extracted runtime, installs stable
wrappers in `/opt/keymasq/bin` and the target user's `~/.local/bin`, writes
system integration under `/etc`, and creates
`/etc/atomic-update.conf.d/keymasq.conf` so SteamOS keeps the `/etc` integration
files across atomic OS updates. On mutable non-SteamOS hosts, it also installs
the `/opt/keymasq/bin/keymasq-record` polkit action under
`/usr/share/polkit-1/actions` when that directory is writable. `/opt/keymasq` is
installed under SteamOS' persistent `/opt` offload mount, not managed by the
atomic update keep-list. It
runs `systemd-sysusers` and `systemd-tmpfiles --create` once during install;
after SteamOS updates, the normal boot-time systemd units reapply the persisted
sysusers/tmpfiles configuration. Installed services and CLI wrappers run from
`/opt/keymasq/runtime/current`, so daemon restarts do not re-extract the
AppImage into a private temp directory. See `docs/STEAMOS.md` for the full
layout and update semantics.

Stable repository publishing copies the AppImage to
`https://repo.keymasq.tools/appimage/`, writes `latest-x86_64.json`, and signs
that manifest with the same GitHub Actions-only package signing key used for
repository metadata and RPM packages. The self-updater refuses signed manifest
replays that would downgrade the installed version unless
`--allow-downgrade` is passed explicitly.

### Nix package and NixOS module

`flake.nix` exposes two important outputs:

- `packages.<system>.default`: a build of the Keymasq package itself
- `nixosModules.default`: a NixOS module that installs the package and wires up
  the system daemon, user session service, udev access, tmpfiles, and the
  generated `/etc/keymasq/security.toml`

This is the most self-contained packaging path in the repository. It is useful
both for Nix users and for developers who want a reproducible build shell.

The flake currently exports Linux builds for:

- `x86_64-linux`
- `aarch64-linux`

Build the package with:

```bash
nix build .#default
```

Open the development shell with:

```bash
nix develop
```

The dev shell provides the repo's standard quality tools and local packaging
helpers, including `pytest`, `ruff`, `basedpyright`, RPM build tools, Python
wheel build tools, `git`, and `ssh`.

For NixOS, the intended consumption path is the module. It exposes:

- `services.keymasq.enable`
- `services.keymasq.package`
- `services.keymasq.installPackage`
- `services.keymasq.securityConfig`

Functional module usage:

```nix
{
  inputs.keymasq.url = "github:nyrda/keymasq";

  outputs = { self, nixpkgs, keymasq, ... }: {
    nixosConfigurations.my-host = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        keymasq.nixosModules.default
        ({ pkgs, ... }: {
          services.keymasq = {
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

- `PKGBUILD` and `keymasq.install` at the repo root for local worktree
  installs
- `packaging/aur/PKGBUILD`, `packaging/aur/keymasq.install`, and
  `packaging/aur/.SRCINFO` for the AUR package repo

The root `PKGBUILD` is for `git clone ... && makepkg -sif` testing. It copies
the current checkout into `$srcdir`, builds a wheel from that local snapshot,
and does not fetch an external archive or VCS source.

The `packaging/aur/` subtree is for release publishing. It expects the
`https://repo.keymasq.tools/releases/keymasq-$pkgver.tar.gz` release tarball URL
and checksum. The GitHub release workflow is the intended path for updating and
pushing that subtree to the AUR repo.

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
- `debian/keymasq.install`: install manifest for the shared payload
- `debian/tests/`: installed-package smoke tests and CLI tests

The Debian package builds the Python package with `pybuild`, stages the shared
payload, and preserves `/etc/keymasq/security.toml` as a Debian conffile so
local edits survive upgrades.

Build the binary package with:

```bash
dpkg-buildpackage -us -uc -b
```

Typical output:

```text
../keymasq_0.1.0-1_all.deb
```

Inspect the resulting package with:

```bash
dpkg-deb -I ../keymasq_0.1.0-1_all.deb
dpkg-deb -c ../keymasq_0.1.0-1_all.deb
```

#### Manual current-worktree build via Debian container

Unlike the RPM flow, the Debian package is not currently set up for a host-side
`nix develop` build. The supported local dev path is to run the same Debian
container job used in GitHub Actions against the current worktree:

```bash
docker run --rm \
  -v "$PWD:/workspace" \
  -w /workspace \
  debian:trixie \
  bash -lc '
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y \
      ca-certificates \
      debhelper \
      dh-sequence-python3 \
      lintian \
      pybuild-plugin-pyproject \
      python3-all \
      python3-build \
      python3-installer \
      python3-setuptools \
      python3-wheel
    bash packaging/debian/ci-build.sh
  '
```

This also builds from the current worktree, so local uncommitted changes in the
packaged files are included. Output artifacts are copied into:

```text
dist/debian/
```

That is the same path used by `packaging/debian/ci-build.sh` and the
`build-deb` GitHub Actions job.

### Fedora COPR and project-hosted RPMs

Fedora COPR is the preferred Fedora release channel. The project-hosted Fedora
repository at `https://repo.keymasq.tools/fedora/$releasever` remains supported
as an alternate install path for users who do not want to enable COPR.

### Fedora and openSUSE RPMs

RPM packaging is driven by `packaging/rpm/metadata.env`,
`scripts/build-packages.sh`, and separate distro-native build paths for Fedora
and openSUSE.

This packaging path exists because the payload is mostly identical across the
RPM-based targets, while dependency names, Python library paths, and build
tooling differ.

That includes the new `uvloop` runtime package recommendation:

- Fedora resolves it through `python3dist(uvloop)` metadata, provided by the
  `python3-uvloop` package
- openSUSE follows the versioned Python package pattern used elsewhere in the
  script, for example `python313-uvloop`

For RPMs this is intentionally a weak dependency rather than a hard one, so the
package remains installable on Fedora releases where `uvloop` is not yet
available in the tested repositories. At runtime, Keymasq still prefers
`uvloop` and logs a warning before falling back to the stdlib `asyncio` loop.

The RPM build flow now splits by distro:

1. Fedora builds a release-specific source tarball and runs a Fedora spec with
   `%pyproject_buildrequires`, `%pyproject_wheel`, `%pyproject_install`, and
   `%pyproject_save_files`
2. openSUSE builds a wheel, stages the shared payload, resolves the target
   Python `site-packages` path, and runs its own rpmbuild wrapper

`scripts/build-packages.sh` builds from the current working tree. Fedora RPMs
are emitted per Fedora release, for example `fc43` and `fc44`, rather than as
a single generic Fedora artifact. The Fedora package itself is
architecture-independent and is built as `noarch`; runtime dependencies remain
resolved by the target Fedora architecture.

Repository publishing keeps those Fedora artifacts in matching release-specific
RPM repositories:

- `https://repo.keymasq.tools/fedora/43`
- `https://repo.keymasq.tools/fedora/44`

Fedora and Fedora-based Atomic desktops should configure the repository with
`$releasever` in the base URL so DNF or `rpm-ostree` only sees the RPM built
for the running Fedora base.

Build RPMs with:

```bash
FEDORA_BUILD_HOST=<fedora-builder> \
OPENSUSE_BUILD_HOST=<opensuse-builder> \
bash scripts/build-packages.sh
```

From the Nix dev shell, the same flow is:

```bash
nix develop -c bash -lc '
  FEDORA_BUILD_HOST=<fedora-builder> \
  OPENSUSE_BUILD_HOST=<opensuse-builder> \
  bash scripts/build-packages.sh
'
```

Common current-worktree examples:

Build only the Fedora RPM using a reachable Fedora host for the actual Fedora
build:

```bash
nix develop -c bash -lc '
  FEDORA_BUILD_HOST=<user@fedora-builder> \
  bash scripts/build-packages.sh
'
```

Build only the openSUSE RPM:

```bash
nix develop -c bash -lc '
  OPENSUSE_BUILD_HOST=<opensuse-builder> \
  bash scripts/build-packages.sh
'
```

If you run the command on Fedora or openSUSE directly, the script can infer the
local target and build that distro's RPM without a remote build host:

```bash
nix develop -c bash -lc 'bash scripts/build-packages.sh'
```

Typical output:

```text
dist/keymasq-0.1.0-1.fc43.noarch.rpm
dist/keymasq-0.1.0-1.opensuse.x86_64.rpm
```

If you only provide one compatible build host, the script builds only that RPM
variant.

Release RPMs are signed after the Fedora and openSUSE build jobs complete.
GitHub Actions imports the armored private key from the
`RPM_SIGNING_KEY_PRIVATE_ASC` repository secret, signs the generated `.rpm`
files with `rpmsign`, verifies the resulting signatures, and publishes the
matching armored public key as `rpm-signing-key.asc` alongside the release
artifacts. The public key is intended to be mirrored at:

```text
https://keymasq.tools/keys/keymasq-rpm-signing-key.asc
```

## Build environments

For general repository work, use the Nix dev shell:

```bash
nix develop
```

That shell covers source checks and the local current-worktree
`scripts/build-packages.sh` RPM flow. Native distro packaging still requires
access to the target distro's metadata when dependency names or Python paths
must be resolved against Fedora or openSUSE.

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
./scripts/check.sh full
```

This wrapper runs `ruff`, `basedpyright`, and the selected pytest scope in the
Nix dev shell. `./scripts/check.sh` defaults to auto-selection from pending and
untracked source changes. Use `./scripts/check.sh keymasqd`,
`./scripts/check.sh session`, or `./scripts/check.sh gui` for focused local
validation, and keep `full` for packaging work, shared-code changes, and broad
refactors.

When the host does not have usable `uinput` access, the same entrypoint can use
the VM backend:

```bash
./scripts/check.sh --vm full
```

### Debian package tests

The Debian package defines two `autopkgtest` tests in `debian/tests/`:

- `pkg-smoke`: validates installed files, services, sockets, and basic runtime
  behavior
- `installed-cli`: runs a curated pytest subset against the installed package

Typical Debian verification flow:

```bash
dpkg-buildpackage -us -uc -b
lintian ../keymasq_0.1.0-1_all.deb
autopkgtest . -- qemu /path/to/debian-autopkgtest.qcow2
```

For libvirt-backed local testing, the repository also provides:

- `packaging/debian/create-libvirt-autopkgtest-vm.sh`
- `packaging/debian/run-autopkgtest-libvirt.sh`

If you need to debug the package inside a guest, install the built `.deb` and
run the test scripts directly:

```bash
sudo apt-get install -y ../keymasq_0.1.0-1_all.deb
sudo sh debian/tests/pkg-smoke
sh debian/tests/installed-cli
```

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

- Fedora and openSUSE RPMs now have separate build paths: Fedora uses a
  Fedora-native spec and release-specific buildroots, while openSUSE keeps its
  own rpmbuild wrapper around the shared staged payload.
- The GNOME bridge extension files are installed by the package, but GNOME users
  must enable the extension explicitly after installation. Packages install the
  files; they do not enable the GNOME Shell extension on the user's behalf.
- Debian packaging is native and repo-local under `debian/`, while RPM
  packaging stays under `packaging/rpm/` with distro-specific rpmbuild
  wrappers. This split is intentional.
