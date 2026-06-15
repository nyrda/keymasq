# Dependency Reference

This document summarizes the dependencies Keymasq needs at runtime, for
packaging, and for development. It complements `docs/INSTALL.md` and
`docs/PACKAGING.md`.

## Scope

There are three different dependency layers in this project:

- Python package dependencies declared in `pyproject.toml`
- System packages required to make those Python packages usable on a desktop
- Feature-specific tools or desktop components that enable optional behavior

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
- `uvloop`
- `python-xlib>=0.33`
- `tomli-w>=1.0.0`

What they are used for:

- `PyGObject`: GTK4 / libadwaita GUI
- `dbus-next`: session D-Bus access for notifications and compositor/session
  integrations
- `evdev`: input device access, recording, capture, and remap runtime
- `uvloop`: default `asyncio` event loop policy
- `python-xlib`: X11 listener support, including cursor position read/write
- `tomli-w`: writing profile, hardware, and superkey TOML files


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
- Path resolution checks the embedded build path first, then `/usr/bin/slurp`,
  `/run/current-system/sw/bin/slurp`, and finally `PATH`
- `SLURP_PATH` overrides auto-detection entirely:
  set it to an absolute path to force that binary, or set it to an empty string
  to disable GUI `slurp` capture

`slurp` is not a universal base runtime dependency. It is a GUI Capture helper
for supported Wayland environments.

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
  - `mypy>=1.0.0`
  - `pytest>=8.0.0`
  - `pytest-asyncio>=0.23.0`
  - `pytest-cov>=5.0.0`
  - `ruff>=0.12.0`

The Nix dev shell in `flake.nix` also provides the system-side pieces needed to
run the test suite and quality checks locally.

## Packaged Runtime Dependencies

This section is a quick packaging summary, not the authoritative source. The
package manifests listed above remain authoritative.

### Arch package

Defined in `PKGBUILD` under `depends`:

- `acl`
- `slurp`
- `python>=3.12`
- `python-dbus-next>=0.2.3`
- `python-evdev>=1.6.0`
- `python-gobject>=3.42.0`
- `python-cairo`
- `python-uvloop`
- `python-tomli-w>=1.0.0`
- `python-xlib>=0.33`
- `gtk4`
- `libadwaita`
- `polkit`
- `systemd`

### Debian package

Defined in `debian/control`:

- `acl`
- `gir1.2-adw-1`
- `gir1.2-gtk-4.0`
- `pkexec`
- `python3-dbus-next`
- `python3-evdev`
- `python3-gi`
- `python3-gi-cairo`
- `python3-tomli-w`
- `python3-uvloop`
- `python3-xlib`
- `systemd`
- `udev`

Defined in `debian/control` under `Recommends`:

- `slurp`

Defined in `debian/control` under `Suggests`:

- `gnome-shell`

### Fedora / openSUSE RPM packaging

Defined by `packaging/rpm/metadata.env` and the distro-specific RPM build
scripts. Fedora additionally relies on Fedora's `%pyproject_*` macros, so it
is built per Fedora release rather than as one cross-release RPM:

- `acl`
- `python3 >= 3.12`
- distro-specific Python package names for:
  - `evdev`
  - `tomli-w`
  - `dbus-next`
  - `python-xlib`
  - `PyGObject`
  - PyGObject Cairo bindings where split from `PyGObject`
- distro-specific GTK4 and libadwaita package names
- `polkit`
- `systemd`

Defined in the generated RPM spec under weak dependencies:

- `slurp`
- distro-specific `uvloop` package name

Verified current RPM dependency naming:

- Fedora metadata uses `python3dist(uvloop)`, which is provided by
  `python3-uvloop`
- openSUSE metadata follows the versioned Python package pattern, for example
  `python313-uvloop`

Keymasq keeps `uvloop` as a weak RPM dependency instead of a hard one because
Fedora 43 does not currently expose a stable `python3-uvloop` package in the
same way Fedora 42 does. RPM installs therefore remain valid without `uvloop`,
and the runtime falls back cleanly with a warning when it is unavailable.

### Nix package and NixOS module

Defined in `flake.nix`:

- Python runtime packages:
  - `dbus-next`
  - `evdev`
  - `tomli-w`
  - `uvloop`
  - `xlib`
  - `pygobject3`
- GUI/system libraries:
  - `gtk4`
  - `libadwaita`
  - icon themes for the wrapped GUI
- build/runtime integration:
  - `gobject-introspection`
  - `wrapGAppsHook4`
- helper path stamping:
  - `slurp` path is embedded into the build
  - `keymasq-record` helper path is embedded into the build

The NixOS module also provisions:

- the `keymasq` system user and group
- systemd units
- tmpfiles rules
- udev ACL setup
- `/etc/keymasq/security.toml`
