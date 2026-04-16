# Installation Guide

## 1. Package Installs (Recommended)

### Arch Linux

Install from the AUR:

```bash
yay -S keymasq
sudo systemctl enable --now keymasqd
systemctl --user enable --now keymasq-session
```

### Arch Linux (Build from checkout)

Build from a checkout and install with pacman. The repo-root `PKGBUILD` is
intended for this flow and packages the current worktree directly:

```bash
git clone https://github.com/nyrda/keymasq.git
cd keymasq
makepkg -sif
```

Then enable services:

```bash
sudo systemctl enable --now keymasqd
systemctl --user enable --now keymasq-session
```

### Debian / Ubuntu / Linux Mint

Add the Keymasq repository and install:

```bash
curl -fsSL https://repo.keymasq.tools/gpg-key.asc \
  | sudo gpg --dearmor -o /etc/apt/keyrings/keymasq.gpg
echo "deb [signed-by=/etc/apt/keyrings/keymasq.gpg arch=all] https://repo.keymasq.tools/debian stable main" \
  | sudo tee /etc/apt/sources.list.d/keymasq.list
sudo apt update
sudo apt install keymasq
sudo systemctl enable --now keymasqd
systemctl --user enable --now keymasq-session
```

Alternatively, download the `.deb` from the
[GitHub release](https://github.com/nyrda/keymasq/releases) and install it
directly with `sudo apt install ./keymasq_*_all.deb`.

### Fedora

Add the Keymasq repository and install:

```bash
sudo tee /etc/yum.repos.d/keymasq.repo << 'EOF'
[keymasq]
name=Keymasq
baseurl=https://repo.keymasq.tools/fedora
enabled=1
gpgcheck=1
gpgkey=https://repo.keymasq.tools/gpg-key.asc
EOF
sudo dnf install keymasq
sudo systemctl enable --now keymasqd
systemctl --user enable --now keymasq-session
```

Alternatively, import the signing key and install the RPM from the
[GitHub release](https://github.com/nyrda/keymasq/releases) directly:

```bash
sudo rpm --import https://repo.keymasq.tools/gpg-key.asc
sudo dnf install ./keymasq-*.fedora.*.rpm
```

### openSUSE Tumbleweed / Leap

Add the Keymasq repository and install:

```bash
sudo rpm --import https://repo.keymasq.tools/gpg-key.asc
sudo zypper addrepo -f --gpgcheck https://repo.keymasq.tools/opensuse keymasq
sudo zypper install keymasq
sudo systemctl enable --now keymasqd
systemctl --user enable --now keymasq-session
```

Alternatively, download the RPM from the
[GitHub release](https://github.com/nyrda/keymasq/releases) and install it
directly with `sudo zypper install ./keymasq-*.opensuse.*.rpm`.

The signing key used for repository metadata and RPM packages is available at
`https://repo.keymasq.tools/gpg-key.asc`. The current fingerprint is:

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
sha256sum keymasq_*_all.deb
grep 'keymasq_.*_all.deb' SHA256SUMS
```

### Verify GitHub artifact attestations

GitHub releases are also accompanied by GitHub Actions build attestations for
the published `.deb`, `.rpm`, `rpm-signing-key.asc`, and `SHA256SUMS` files. If
you have the GitHub CLI installed, you can verify that an artifact was produced
by the Keymasq release workflow:

```bash
gh attestation verify ./keymasq_*_all.deb -R nyrda/keymasq
```

Use the same command shape for RPMs or `SHA256SUMS`:

```bash
gh attestation verify ./keymasq-*.fedora.*.rpm -R nyrda/keymasq
gh attestation verify ./rpm-signing-key.asc -R nyrda/keymasq
gh attestation verify ./SHA256SUMS -R nyrda/keymasq
```

### NixOS

NixOS support is provided through the flake module. Add the input, import the
module, enable the service, and rebuild:

```nix
{
  inputs.keymasq.url = "github:nyrda/keymasq";

  outputs = { self, nixpkgs, keymasq, ... }: {
    nixosConfigurations.my-host = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        keymasq.nixosModules.default
        ({ ... }: {
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

```bash
sudo nixos-rebuild switch --flake .#my-host
```

This installs the package, enables `keymasqd`, enables the
`keymasq-session` user service for graphical sessions, and generates
`/etc/keymasq/security.toml`.

## 2. Advanced: Manual Install

This section is for advanced users with custom setups. Most users should use
the packaged installs above.

For a working manual install, make sure the following pieces exist on the
system:

- a Python 3.12+ environment containing Keymasq and its Python dependencies
- GTK4 and libadwaita runtime libraries for the GUI
- `slurp` available for wlroots/COSMIC cursor acquisition and GUI point-pick flows
- the Keymasq executables available to the system or the users who will run them: `keymasq`, `keymasqd`, and `keymasq-session`
- a long-running launcher for the privileged daemon process
- a per-user launcher for the session process in graphical sessions
- a dedicated `keymasq` service user, or an equivalent privileged runtime identity for `keymasqd`
- the required runtime and state directories: `/run/keymasq` and `/var/lib/keymasq`
- a security policy file at `/etc/keymasq/security.toml` if you want explicit policy configuration
- input and `uinput` device access set up for the privileged daemon identity
- any compositor-specific integration required by your desktop environment

Manual installs do not need to use `systemd` specifically. Any equivalent
service manager or launcher arrangement is fine as long as `keymasqd` runs as
the privileged service identity and `keymasq-session` runs in the user session.

### Recording and capture unlock

Packaged installs handle this automatically. For manual installs, if macro
recording prompts do not appear or fail, you can disable the unlock requirement
in `/etc/keymasq/security.toml`:

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
systemctl status keymasqd
systemctl --user status keymasq-session
```

For non-`systemd` manual setups, verify with your own service manager or launch
method that:

- `keymasqd` is running under the intended privileged identity
- `keymasq-session` is running in the user session
- `keymasq` can connect and profile activation works on real device input

For debugging service startup, permissions, compositor integration, or verbose
logging, see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

For development work, use the Nix-based flow in [DEVELOPMENT.md](../DEVELOPMENT.md).

## 4. Diagnostics (Optional)

You can enable keymasqd latency diagnostics at runtime:

```bash
keymasq diagnostics on --interval 5
journalctl -u keymasqd -f
```

Disable diagnostics:

```bash
keymasq diagnostics off
```

## 5. Package Lifecycle

### Upgrade

If you installed from the Keymasq repository, upgrade with your package
manager:

```bash
sudo apt update && sudo apt upgrade keymasq
sudo dnf upgrade keymasq
sudo zypper update keymasq
```

For manual GitHub release installs, download the newer package and install it
over the existing one:

```bash
sudo apt install ./keymasq_*_all.deb
sudo dnf install ./keymasq-*.fedora.*.rpm
sudo zypper install ./keymasq-*.opensuse.*.rpm
```

After upgrading, make sure both services are restarted:

```bash
sudo systemctl restart keymasqd
systemctl --user restart keymasq-session
```

### Uninstall

Package removal does not remove user profiles or hardware configuration stored
under `~/.config/keymasq/`.

Packaged installs may also leave system-level configuration in place, notably
`/etc/keymasq/security.toml`.

Disable and stop both services:

```bash
sudo systemctl disable --now keymasqd
systemctl --user disable --now keymasq-session
```

### Rollback

Rolling back to an earlier packaged release is done by reinstalling the older
package version with your package manager. Before rolling back:

- keep a backup of `~/.config/keymasq/`
- keep a backup of `/etc/keymasq/security.toml` if you edited it
- verify that your stored profiles and config remain compatible with the older
  release

### Manual-install cleanup

If you set up Keymasq manually rather than through a native package,
uninstall is partly manual:

- remove the Python environment or package location that provides Keymasq
- remove any service definitions, wrappers, or launcher integrations you added
- remove any manually installed runtime-policy, udev, or privilege-management integration you added
- keep or delete `~/.config/keymasq/` depending on whether you want to retain
  profiles and hardware config
