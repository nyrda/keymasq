# Dependency Reference

This document summarizes the dependencies Keyforge needs at runtime, for
packaging, and for development. It complements `docs/INSTALL.md` and
`docs/PACKAGING.md`.

## Scope

There are three different dependency layers in this project:

- Python package dependencies declared in `pyproject.toml`
- System packages required to make those Python packages usable on a desktop
- Feature-specific tools or desktop components that enable optional behavior

If you are packaging Keyforge, check the package definitions directly:

- `pyproject.toml`
- `PKGBUILD`
- `debian/control`
- `nfpm.yaml`
- `flake.nix`

## Python Version

- Minimum supported Python version: `3.12`

Keyforge uses the Python 3.12 standard library `tomllib` for TOML reads, so it
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
- `python-xlib`: X11 listener support
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

Privileged recording/unlock flow:

- `polkit`
- `pkexec`

Notes:

- `PyGObject` is a Python dependency, but it still requires the underlying GTK
  and introspection libraries from the operating system.
- `keyforge-record` is not a third-party dependency. It is a Keyforge-provided
  helper script installed as part of the package.
- The `keyforged` daemon relies on system integration from the package or local
  setup: service units, a `keyforge` system user, tmpfiles, and udev ACL rules.

## Feature-Specific Dependencies

These are optional in the sense that Keyforge can run without them, but the
related feature will be unavailable or degraded.

### Compositor and desktop support

- Hyprland: supported with the base install when running under Hyprland
- Generic wlroots Wayland: supported with the base install on compositors that
  expose the required foreign-toplevel protocol
- KDE Plasma Wayland: supported with the base install
- COSMIC Wayland: supported with the base install
- GNOME Wayland: requires the Keyforge GNOME Shell bridge extension in
  `gnome-extension/`
- X11: requires `python-xlib`, which is part of the base Python dependency set

No extra Python package is currently required specifically for Hyprland, KDE,
COSMIC, wlroots Wayland, or GNOME beyond the base runtime set. The differentiator
is the compositor/session environment itself.

### Pointer capture helpers

- `slurp` is used only on compatible compositors:
  `hyprland`, `wayland-wlr`, `kde`, and `cosmic`
- On `wayland-wlr` and `cosmic`, `slurp` is the cursor-acquisition path used by
  the listener for recording start-position capture and similar pointer reads
- On unsupported compositors, `slurp` is not used

So `slurp` is not a universal base runtime dependency, but it is a real
feature/runtime dependency for supported Wayland environments rather than just a
GUI convenience.

### Recording unlock helper

- `keyforge-record` must be installed alongside the rest of Keyforge
- the matching Polkit policy must be installed
- the helper path and Polkit policy must agree on the same absolute executable
  path

See `docs/SECURITY.md` and `docs/PACKAGING.md` for details.

## Development and Test Dependencies

Defined in `pyproject.toml` under `[project.optional-dependencies]`:

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
- `python-tomli-w>=1.0.0`
- `python-xlib>=0.33`
- `gtk4`
- `libadwaita`
- `polkit`
- `systemd`

Defined in `PKGBUILD` under `optdepends`:

- `hyprland` for Hyprland-specific window integration

### Debian package

Defined in `debian/control`:

- `acl`
- `gir1.2-adw-1`
- `gir1.2-gtk-4.0`
- `pkexec | policykit-1 | polkitd`
- `python3-dbus-next`
- `python3-evdev`
- `python3-gi`
- `python3-tomli-w`
- `python3-xlib`
- `systemd`
- `udev`

Defined in `debian/control` under `Recommends`:

- `slurp`

Defined in `debian/control` under `Suggests`:

- `gnome-shell`

### Fedora / openSUSE RPM packaging

Defined in `nfpm.yaml`:

- `acl`
- `python3 >= 3.12`
- distro-specific Python package names for:
  - `evdev`
  - `tomli-w`
  - `dbus-next`
  - `python-xlib`
  - `PyGObject`
- distro-specific GTK4 and libadwaita package names
- `polkit`
- `systemd`

Defined in `nfpm.yaml` under `recommends`:

- `slurp`

### Nix package and NixOS module

Defined in `flake.nix`:

- Python runtime packages:
  - `dbus-next`
  - `evdev`
  - `tomli-w`
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
  - `keyforge-record` helper path is embedded into the build

The NixOS module also provisions:

- the `keyforge` system user and group
- systemd units
- tmpfiles rules
- udev ACL setup
- `/etc/keyforge/security.toml`
