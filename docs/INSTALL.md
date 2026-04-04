# Installation Guide

## 1. Package Installs (Recommended)

### Arch Linux

Install from the AUR:

```bash
yay -S keyforge
sudo systemctl enable --now keyforged
systemctl --user enable --now keyforge-session
```

### Arch Linux (Build from checkout)

Build from a checkout and install with pacman. The repo-root `PKGBUILD` is
intended for this flow and packages the current worktree directly:

```bash
git clone https://github.com/nyrda/keyforge.git
cd keyforge
makepkg -sif
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

Import the Keyforge RPM signing key once, verify the fingerprint, then install
the current Fedora RPM from the GitHub release:

```bash
curl -fsSLO https://keyforge.tools/keys/keyforge-rpm-signing-key.asc
gpg --show-keys --fingerprint keyforge-rpm-signing-key.asc
sudo rpm --import keyforge-rpm-signing-key.asc
rpm --checksig ./keyforge-*.fedora.*.rpm
sudo dnf install ./keyforge-*.fedora.*.rpm
sudo systemctl enable --now keyforged
systemctl --user enable --now keyforge-session
```

### openSUSE Tumbleweed / Leap

Import the Keyforge RPM signing key once, verify the fingerprint, then install
the current openSUSE RPM from the GitHub release:

```bash
curl -fsSLO https://keyforge.tools/keys/keyforge-rpm-signing-key.asc
gpg --show-keys --fingerprint keyforge-rpm-signing-key.asc
sudo rpm --import keyforge-rpm-signing-key.asc
rpm --checksig ./keyforge-*.opensuse.*.rpm
sudo zypper install ./keyforge-*.opensuse.*.rpm
sudo systemctl enable --now keyforged
systemctl --user enable --now keyforge-session
```

The current RPM signing key fingerprint is:

```text
733B FA24 A526 857B 06E7  A5D9 E002 1F70 BA1C 66DE
```

### Verify GitHub release checksums

GitHub releases include a `SHA256SUMS` file for the published `.deb` and `.rpm`
artifacts, plus the published `rpm-signing-key.asc`. After downloading the
package you want to install and the matching `SHA256SUMS` file, verify them
from the same directory:

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
the published `.deb`, `.rpm`, `rpm-signing-key.asc`, and `SHA256SUMS` files. If
you have the GitHub CLI installed, you can verify that an artifact was produced
by the Keyforge release workflow:

```bash
gh attestation verify ./keyforge_*_all.deb -R nyrda/keyforge
```

Use the same command shape for RPMs or `SHA256SUMS`:

```bash
gh attestation verify ./keyforge-*.fedora.*.rpm -R nyrda/keyforge
gh attestation verify ./rpm-signing-key.asc -R nyrda/keyforge
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

## 2. Advanced: Manual Install

This section is for advanced users with custom setups. Most users should use
the packaged installs above.

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

### Recording and capture unlock

Packaged installs handle this automatically. For manual installs, if macro
recording prompts do not appear or fail, you can disable the unlock requirement
in `/etc/keyforge/security.toml`:

```toml
[recording_guard]
unlock_required = false
```

If you want to remap left or right mouse click (disabled by default to prevent
locking yourself out of the desktop):

```toml
[gui]
allow_left_right_click_remap = true
```

For all available settings, see [SECURITY.md](SECURITY.md) and
[examples/security.toml](../examples/security.toml).

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

After upgrading, make sure both services are restarted:

```bash
sudo systemctl restart keyforged
systemctl --user restart keyforge-session
```

### Uninstall

Package removal does not remove user profiles or hardware configuration stored
under `~/.config/keyforge/`.

Packaged installs may also leave system-level configuration in place, notably
`/etc/keyforge/security.toml`.

Disable and stop both services:

```bash
sudo systemctl disable --now keyforged
systemctl --user disable --now keyforge-session
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
