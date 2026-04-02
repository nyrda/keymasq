# Installation Guide

This guide covers packaged installs and source installs.

Packaged installs are the recommended path. Dependency details and change
history are tracked in `docs/DEPENDENCIES.md`.

Python source installs require Python 3.12+.

## 1. Package Installs (Recommended)

### Arch Linux

Install from the AUR:

```bash
yay -S keyforge
sudo systemctl enable --now keyforged
systemctl --user enable --now keyforge-session
```

### Arch Linux (Build from checkout)

Build from a checkout and install with pacman:

```bash
git clone https://github.com/nyrda/keyforge.git
cd keyforge
makepkg -si
```

Then enable services:

```bash
sudo systemctl enable --now keyforged
systemctl --user enable --now keyforge-session
```

### Debian / Ubuntu / Linux Mint

Download the current `.deb` from the GitHub release, then install it:

```bash
sudo apt update
sudo apt install ./keyforge_*_all.deb
sudo systemctl enable --now keyforged
systemctl --user enable --now keyforge-session
```

### Fedora

Download the current Fedora RPM from the GitHub release, then install it:

```bash
sudo dnf install ./keyforge-*.fedora.*.rpm
sudo systemctl enable --now keyforged
systemctl --user enable --now keyforge-session
```

### openSUSE Tumbleweed / Leap

Download the current openSUSE RPM from the GitHub release, then install it:

```bash
sudo zypper install ./keyforge-*.opensuse.*.rpm
sudo systemctl enable --now keyforged
systemctl --user enable --now keyforge-session
```

### Verify GitHub release checksums

GitHub releases include a `SHA256SUMS` file for the published `.deb` and `.rpm`
artifacts. After downloading the package you want to install and the matching
`SHA256SUMS` file, verify them from the same directory:

```bash
sha256sum -c --ignore-missing SHA256SUMS
```

You can also verify a single downloaded artifact directly:

```bash
sha256sum keyforge_*_all.deb
grep 'keyforge_.*_all.deb' SHA256SUMS
```

### Verify GitHub artifact attestations

GitHub releases are also accompanied by GitHub Actions build attestations for
the published `.deb`, `.rpm`, and `SHA256SUMS` files. If you have the GitHub
CLI installed, you can verify that an artifact was produced by the Keyforge
release workflow:

```bash
gh attestation verify ./keyforge_*_all.deb -R nyrda/keyforge
```

Use the same command shape for RPMs or `SHA256SUMS`:

```bash
gh attestation verify ./keyforge-*.fedora.*.rpm -R nyrda/keyforge
gh attestation verify ./SHA256SUMS -R nyrda/keyforge
```

### NixOS

NixOS support is provided through the flake module. Add the input, import the
module, enable the service, and rebuild:

```nix
{
  inputs.keyforge.url = "github:nyrda/keyforge";

  outputs = { self, nixpkgs, keyforge, ... }: {
    nixosConfigurations.my-host = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        keyforge.nixosModules.default
        ({ ... }: {
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

```bash
sudo nixos-rebuild switch --flake .#my-host
```

This installs the package, enables `keyforged`, enables the
`keyforge-session` user service for graphical sessions, and generates
`/etc/keyforge/security.toml`.

## 2. Manual Install Requirements

Manual installs are intended for advanced custom setups. This document does not
prescribe one specific tool or command sequence for creating the Python
environment or registering services.

For a working manual install, make sure the following pieces exist on the
system:

- a Python 3.12+ environment containing Keyforge and its Python dependencies
- GTK4 and libadwaita runtime libraries for the GUI
- `slurp` available for wlroots/COSMIC cursor acquisition and GUI point-pick flows
- the Keyforge executables available to the system or the users who will run them: `keyforge`, `keyforged`, and `keyforge-session`
- a long-running launcher for the privileged daemon process
- a per-user launcher for the session process in graphical sessions
- a dedicated `keyforge` service user, or an equivalent privileged runtime identity for `keyforged`
- the required runtime and state directories: `/run/keyforge` and `/var/lib/keyforge`
- a security policy file at `/etc/keyforge/security.toml` if you want explicit policy configuration
- input and `uinput` device access set up for the privileged daemon identity
- any compositor-specific integration required by your desktop environment

Manual installs do not need to use `systemd` specifically. Any equivalent
service manager or launcher arrangement is fine as long as `keyforged` runs as
the privileged service identity and `keyforge-session` runs in the user session.

### Recording and capture unlock integration

The packaged unlock flow uses `keyforge-record` and Polkit. Manual installs do
not need to replicate that.

For manual installs, the simpler recommendation is to configure
`/etc/keyforge/security.toml` for your setup. If you are not providing the
packaged Polkit-based unlock flow, set `[recording_guard] unlock_required =
false` there.

If you intentionally want to remap left or right click from the GUI, also set
`[gui] allow_left_right_click_remap = true`. Keyforge keeps this disabled by
default because breaking primary/secondary click can make the desktop UI hard
to recover from.

For the available security policy settings, see [docs/SECURITY.md](SECURITY.md)
and [examples/security.toml](../examples/security.toml).

## 3. Verification

For packaged installs or `systemd`-based manual setups:

```bash
systemctl status keyforged
systemctl --user status keyforge-session
```

For non-`systemd` manual setups, verify with your own service manager or launch
method that:

- `keyforged` is running under the intended privileged identity
- `keyforge-session` is running in the user session
- `keyforge` can connect and profile activation works on real device input

For debugging service startup, permissions, compositor integration, or verbose
logging, see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

For development work, use the Nix-based flow in [DEVELOPMENT.md](../DEVELOPMENT.md).

## 4. Diagnostics (Optional)

You can enable keyforged latency diagnostics at runtime:

```bash
keyforge diagnostics on --interval 5
journalctl -u keyforged -f
```

Disable diagnostics:

```bash
keyforge diagnostics off
```

## 5. Package Lifecycle

### Upgrade

Upgrade packaged installs using the normal package manager flow for your
distribution, or install a newer GitHub release package over the existing one:

```bash
sudo apt install ./keyforge_*_all.deb
sudo dnf install ./keyforge-*.fedora.*.rpm
sudo zypper install ./keyforge-*.opensuse.*.rpm
```

After upgrading, verify that both services are still active:

```bash
systemctl status keyforged
systemctl --user status keyforge-session
```

### Uninstall

Package removal does not remove user profiles or hardware configuration stored
under `~/.config/keyforge/`.

Packaged installs may also leave system-level configuration in place, notably
`/etc/keyforge/security.toml`.

RPM package removal also disables and stops the `keyforged` system service. On
all package types, it is a good idea to confirm service state after removal:

```bash
systemctl status keyforged
systemctl --user status keyforge-session
```

### Rollback

Rolling back to an earlier packaged release is done by reinstalling the older
package version with your package manager. Before rolling back:

- keep a backup of `~/.config/keyforge/`
- keep a backup of `/etc/keyforge/security.toml` if you edited it
- verify that your stored profiles and config remain compatible with the older
  release

### Manual-install cleanup

If you set up Keyforge manually rather than through a native package,
uninstall is partly manual:

- remove the Python environment or package location that provides Keyforge
- remove any service definitions, wrappers, or launcher integrations you added
- remove any manually installed runtime-policy, udev, or privilege-management integration you added
- keep or delete `~/.config/keyforge/` depending on whether you want to retain
  profiles and hardware config
