# Dependency Reference

This document summarizes the dependencies Keymasq needs at runtime, for
packaging, and for development. It complements `docs/INSTALL.md` and
`docs/PACKAGING.md`.

## Scope

There are four different dependency layers in this project:

- required Python package dependencies declared in `pyproject.toml`, plus
  optional Python extras such as the `speedups` extra for `uvloop`
- system packages required to make those Python packages usable on a desktop
  (GTK4, libadwaita, introspection data, polkit)
- packaged dependencies: what each maintained package family actually
  installs, which may promote an optional Python package to
  installed-by-default
- optional compositor helpers that enable specific desktop features, such as
  `slurp` and the GNOME Shell bridge extension

If you are packaging Keymasq, check the package definitions directly:

- `pyproject.toml`
- `PKGBUILD`
- `debian/control`
- `packaging/rpm/metadata.env`
- `packaging/rpm/build-fedora-rpm.sh`
- `packaging/rpm/build-opensuse-rpm.sh`
- `flake.nix`

## Python Version

- Minimum supported Python version: `3.12`

Keymasq uses the Python 3.12 standard library `tomllib` for TOML reads, so it
does not depend on the older `tomli` package.

## Base Python Runtime Dependencies

Defined in `pyproject.toml` under `[project].dependencies`:

- `PyGObject>=3.42.0`
- `dbus-next>=0.2.3`
- `evdev>=1.6.0`
- `python-xlib>=0.33`
- `tomli-w>=1.0.0`

What they are used for:

- `PyGObject`: GTK4 / libadwaita GUI
- `dbus-next`: session D-Bus access for notifications and compositor/session
  integrations
- `evdev`: input device access, recording, capture, and remap runtime
- `python-xlib`: X11 listener support, including cursor position read/write
- `tomli-w`: writing profile, hardware, and superkey TOML files

### Optional Python speedup: uvloop

`uvloop` is not a required base dependency. It is declared in the `speedups`
extra in `pyproject.toml`. When it is importable, `keymasqd` and
`keymasq-session` install `uvloop.EventLoopPolicy` as the default `asyncio`
policy; when it is missing or broken, they log a warning and fall back to the
stdlib event loop. No feature is lost without it — only latency/jitter
headroom (see `docs/PERFORMANCE.md`).

Most maintained packages install it by default anyway:

| Package family | uvloop |
| -------------- | ------ |
| Arch / AUR | hard dependency (`python-uvloop`) |
| Debian | hard dependency (`python3-uvloop`) |
| Fedora RPM | weak dependency (`Recommends: python3dist(uvloop)`) |
| openSUSE RPM | weak dependency (versioned `pythonXYZ-uvloop`) |
| AppImage / SteamOS | bundled |
| Nix / NixOS | included in the wrapped Python environment |

### python-evdev compatibility lanes

The default Nix development and check commands use the current `evdev` package
from the pinned `nixpkgs` input. Compatibility lanes are available for testing
the Ubuntu 24.04 package range explicitly:

- `nix develop .#ci-evdev161 -c pytest ...`
- `nix develop .#ci-evdev170 -c pytest ...`
- `nix build .#checks.x86_64-linux.pytest-vm-evdev161`
- `nix build .#checks.x86_64-linux.pytest-vm-evdev170`
- `nix build .#checks.x86_64-linux.daemon-session-integration-test-evdev161`
- `nix build .#checks.x86_64-linux.daemon-session-integration-test-evdev170`

`evdev 1.6.x` supports `InputDevice.input_props()` and
`UInput(..., input_props=...)`, but it does not support
`UInput(..., max_effects=...)`. Keymasq omits `max_effects` on that runtime, so
passthrough devices can still be created. Force-feedback passthrough remains
available, but the virtual device's advertised maximum effect count cannot be
capped to the physical device's exact value until `evdev 1.7.0+`.


## System Runtime Dependencies

These are not fully expressed by Python package metadata alone, but they are
required for a usable desktop install.

Core system integration:

- `systemd`
- `udev`
- `acl`

Desktop GUI stack:

- GTK4
- libadwaita
- GObject introspection data for GTK4 and libadwaita

Privileged capture unlock and macro recording opt-in flow:

- `polkit`
- `pkexec`

Notes:

- `PyGObject` is a Python dependency, but it still requires the underlying GTK
  and introspection libraries from the operating system.
- `keymasq-record` is not a third-party dependency. It is a Keymasq-provided
  helper script installed as part of the package.
- The `keymasqd` daemon relies on system integration from the package or local
  setup: service units, a `keymasq` system user, tmpfiles, and udev ACL rules.

## Feature-Specific Dependencies

These are optional in the sense that Keymasq can run without them, but the
related feature will be unavailable or degraded.

### Compositor and desktop support

- Hyprland: supported with the base install when running under Hyprland
- Niri: supported with the base install when running under Niri
- Generic wlroots Wayland: supported with the base install on compositors that
  expose the required foreign-toplevel protocol
- KDE Plasma Wayland: supported with the base install
- COSMIC Wayland: supported with the base install
- GNOME Wayland: requires the Keymasq GNOME Shell bridge extension in
  `gnome-extension/`
- X11: requires `python-xlib`, which is part of the base Python dependency set

No extra Python package is currently required specifically for Hyprland, Niri,
KDE, COSMIC, wlroots Wayland, or GNOME beyond the base runtime set. The
differentiator is the compositor/session environment itself.

### Pointer capture helpers

- Session cursor-position reads on generic Wayland use an internal
  `zwlr_layer_shell_v1` + `zxdg_output_manager_v1` backend; they do not spawn
  `slurp`
- `slurp` is used only by GUI point-picking Capture on compatible compositors:
  `hyprland`, `wayland`, `wayland-wlr`, `wayland-layer-shell`, `kde`,
  `cosmic`, and `niri`
- On unsupported compositors, GUI `slurp` capture is not used
- AppImage builds bundle `slurp` and prefer the extracted AppImage runtime path
- Other builds check the embedded build path first, then `/usr/bin/slurp`,
  `/run/current-system/sw/bin/slurp`, and finally `PATH`
- `SLURP_PATH` overrides auto-detection entirely:
  set it to an absolute path to force that binary, or set it to an empty string
  to disable GUI `slurp` capture

`slurp` is not a universal base runtime dependency. It is a GUI Capture helper
for supported Wayland environments.

### Browser GUI backend

- The AppImage bundles the checksum-pinned `nyrda/gtk-brotway` Arch overlay.
- gtk-brotway supplies a private Broadway-only `libgtk-4`, `gtk4-broadwayd`,
  its launcher, and debug menu; it is activated only by `gtk4-brotway-run`.
- Native packages do not depend on or install gtk-brotway.

### Capture unlock helper

- `keymasq-record` must be installed alongside the rest of Keymasq
- the matching Polkit policy must be installed
- the helper path and Polkit policy must agree on the same absolute executable
  path

See `docs/SECURITY.md` and `docs/PACKAGING.md` for details.

## Development and Test Dependencies

Defined in `pyproject.toml` under `[project.optional-dependencies]`:

- `speedups`
  - `uvloop`
- `test`
  - `pytest>=8.0.0`
  - `pytest-asyncio>=0.23.0`
  - `pytest-cov>=5.0.0`
- `dev`
  - `basedpyright>=1.38.2`
  - `pytest>=8.0.0`
  - `pytest-asyncio>=0.23.0`
  - `pytest-cov>=5.0.0`
  - `ruff>=0.12.0`

The Nix dev shell in `flake.nix` also provides the system-side pieces needed to
run the test suite and quality checks locally.

## Packaged Runtime Dependencies

The package manifests are authoritative, and this document intentionally does
not duplicate their full dependency lists. Check them directly:

- Arch / AUR: `PKGBUILD` and `packaging/aur/PKGBUILD` (`depends`)
- Debian: `debian/control` (`Depends` / `Recommends` / `Suggests`)
- Fedora / openSUSE: `packaging/rpm/build-fedora-rpm.sh` and
  `packaging/rpm/build-opensuse-rpm.sh` (driven by
  `packaging/rpm/metadata.env`)
- AppImage / SteamOS: `packaging/appimage/get-dependencies.sh`
- Nix / NixOS: `flake.nix`

Every family covers the same required core: the base Python dependencies
above, GTK4 and libadwaita with their introspection data, polkit/pkexec,
systemd and udev integration, and `acl` for the `setfacl`-based device access
rules. The families differ only in how they classify the optional pieces:

| Package family | `uvloop` | `slurp` |
| -------------- | -------- | ------- |
| Arch / AUR | hard dependency | hard dependency |
| Debian | hard dependency | `Recommends` |
| Fedora RPM | `Recommends` | `Recommends` |
| openSUSE RPM | `Recommends` | `Recommends` |
| AppImage / SteamOS | bundled | bundled |
| Nix / NixOS | in the wrapped Python environment | path-stamped into the build |

Debian additionally `Suggests: gnome-shell` for the GNOME bridge extension
use case.

### RPM packaging notes

Fedora relies on Fedora's `%pyproject_*` macros, so RPMs are built per Fedora
release rather than as one cross-release RPM. Fedora resolves `uvloop`
through `python3dist(uvloop)` metadata (provided by `python3-uvloop`);
openSUSE follows its versioned Python package pattern, for example
`python313-uvloop`.

Keymasq keeps `uvloop` as a weak RPM dependency instead of a hard one because
Fedora 43 does not currently expose a stable `python3-uvloop` package in the
same way Fedora 42 does. RPM installs therefore remain valid without `uvloop`,
and the runtime falls back cleanly with a warning when it is unavailable.

### Nix package and NixOS module

Beyond the Python environment, the Nix build wraps the GUI with
`gobject-introspection` / `wrapGAppsHook4` and icon themes, and embeds the
`slurp` and `keymasq-record` helper paths into the build.

The NixOS module also provisions:

- the `keymasq` system user and group
- systemd units
- tmpfiles rules
- udev ACL setup
- `/etc/keymasq/security.toml`
