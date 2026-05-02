# Installation Guide

Pick your distribution below. Keymasq runs as two services: a
privileged system daemon that accesses input devices, and a per-user
service that manages your profiles, window tracking, and GUI requests.

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
baseurl=https://repo.keymasq.tools/fedora/$releasever
enabled=1
gpgcheck=1
gpgkey=https://repo.keymasq.tools/gpg-key.asc
metadata_expire=1h
EOF
sudo dnf install keymasq
sudo systemctl enable --now keymasqd
systemctl --user enable --now keymasq-session
```

Alternatively, import the signing key and install the RPM from the
[GitHub release](https://github.com/nyrda/keymasq/releases):

```bash
sudo rpm --import https://repo.keymasq.tools/gpg-key.asc
sudo dnf install ./keymasq-*.fc*.rpm
```

### Bazzite

Bazzite is supported through Fedora RPM layering. Add the Fedora-versioned
Keymasq repository and layer the package with `rpm-ostree` so future Keymasq
updates can arrive through normal Bazzite upgrades:

```bash
sudo tee /etc/yum.repos.d/keymasq.repo << 'EOF'
[keymasq]
name=Keymasq
baseurl=https://repo.keymasq.tools/fedora/$releasever
enabled=1
gpgcheck=1
gpgkey=https://repo.keymasq.tools/gpg-key.asc
metadata_expire=1h
EOF
sudo rpm-ostree install keymasq
systemctl reboot
```

If `rpm-ostree` reports a 404 for an older Keymasq RPM after the repository has
changed, clear cached rpm-md metadata and retry:

```bash
sudo rpm-ostree cleanup -m
sudo rpm-ostree install keymasq
systemctl reboot
```

After reboot, enable the services:

```bash
sudo systemctl enable --now keymasqd
systemctl --user enable --now keymasq-session
```

Alternatively, download the Fedora RPM that matches the Fedora base used by
your Bazzite image and layer it manually. For example, Bazzite 43 should use the
`fc43` RPM:

```bash
sudo rpm-ostree install ./keymasq-*.fc43.*.rpm
systemctl reboot
```

Bazzite follows the normal Atomic Desktop model: package layering changes take
effect after reboot. Locally layered RPMs are not updated automatically by the
Keymasq repository, so prefer the repository-backed install unless you are
testing a specific release artifact.

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
AC46 70B9 328E B2EA 468E  8FFF E3FD 12BD B158 EBE4
```

### NixOS

Use the NixOS module from the Keymasq flake. This complete example installs the
GUI/CLI package, starts `keymasqd`, starts `keymasq-session` in graphical user
sessions, and writes `/etc/keymasq/security.toml` from the `securityConfig`
settings below:

```nix
{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.keymasq.url = "github:nyrda/keymasq";

  outputs = { self, nixpkgs, keymasq }: {
    nixosConfigurations.desktop = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        keymasq.nixosModules.default
        {
          services.keymasq = {
            enable = true;
            installPackage = true;
            securityConfig = {
              daemon_allowed_uids = [];
              session_allowed_uids = [];

              macro.exec_timeout_max_ms = 30000;

              gui = {
                emergency_cancel_combo_enabled = true;
              };

              recording_guard = {
                unlock_required = true;
                macro_edit_requires_unlock = false;
              };

              session_command_acl.client = [];
              daemon_command_acl.session = [];
            };
          };
        }
      ];
    };
  };
}
```

```bash
sudo nixos-rebuild switch --flake .#desktop
```

To change the policy, edit `services.keymasq.securityConfig` and rebuild. The
default `gui.emergency_cancel_combo_enabled = true;` reserves `Ctrl+Alt+Esc` on
grabbed keyboards as an emergency combo.

### GNOME Wayland: enable the Shell bridge

GNOME support requires the Keymasq GNOME Shell bridge extension. Packaged
installs include the extension files, but they do not enable the extension for
you.

If you installed Keymasq while already logged into GNOME, log out and back in
once so GNOME Shell rescans system extensions. Then enable the bridge and
restart the session service:

```bash
gnome-extensions enable gnome-bridge@keymasq.tools
systemctl --user restart keymasq-session
```

Verify that GNOME Shell sees the extension:

```bash
gnome-extensions info gnome-bridge@keymasq.tools
```

Without the bridge, Keymasq still runs on GNOME, but window-aware profiles,
pointer-position features, and GNOME compositor actions are unavailable. For
details and manual-install steps, see [GNOME.md](GNOME.md).

## 2. Advanced: Manual Install

This section is for advanced users with custom setups. Most users should use
the packaged installs above.

The following is a requirements checklist, not a step-by-step tutorial. If
you're doing a manual install, you likely already know how to set up services
and permissions on your system.

For a working manual install, make sure the following pieces exist on the
system:

- a Python 3.12+ environment containing Keymasq and its Python dependencies
- `uvloop` installed if you want the preferred async runtime for
  `keymasqd` and `keymasq-session`
- GTK4 and libadwaita runtime libraries for the GUI
- `slurp` available for wlroots/COSMIC cursor acquisition and GUI point-pick flows
- the Keymasq executables available to the system or the users who will run them: `keymasq`, `keymasqd`, and `keymasq-session`
- a long-running launcher for the privileged daemon process
- a per-user launcher for the session process in graphical sessions
- a dedicated `keymasq` service user, or an equivalent privileged runtime identity for `keymasqd`
- the required runtime and state directories: `/run/keymasq` and `/var/lib/keymasq`
- a security policy file at `/etc/keymasq/security.toml` if you want explicit policy configuration
- input and `uinput` device access set up for the privileged daemon identity
- any compositor-specific integration required by your desktop environment,
  such as the GNOME Shell bridge extension on GNOME Wayland

Manual installs do not need to use `systemd` specifically. Any equivalent
service manager or launcher arrangement is fine as long as `keymasqd` runs as
the privileged service identity and `keymasq-session` runs in the user session.

If `uvloop` is missing or fails to import, `keymasqd` and `keymasq-session`
still start and fall back to the default `asyncio` event loop policy. They log
a warning when this happens so the missing optimization is visible.

### Recording and capture unlock

Packaged installs handle this automatically. For manual installs, if macro
recording unlock requests do not appear or fail, you can disable the unlock
requirement in `/etc/keymasq/security.toml`:

```toml
[recording_guard]
unlock_required = false
```

`Ctrl+Alt+Esc` is reserved by default as an emergency combo while Keymasq has a
keyboard grabbed. One tap cancels macro playback; a double tap releases all
grabbed devices and asks the session to reapply active profiles. You can
disable it if you really need that exact combo:

```toml
[gui]
emergency_cancel_combo_enabled = false
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
logging, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

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
sudo dnf install ./keymasq-*.fc*.rpm
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

## 6. Verifying GitHub Releases (Optional)

If you download packages directly from GitHub releases instead of the
repository, you can verify their integrity.

### Checksums

GitHub releases include a `SHA256SUMS` file for the published `.deb` and `.rpm`
artifacts. After downloading the package and the `SHA256SUMS` file, verify from
the same directory:

```bash
sha256sum -c --ignore-missing SHA256SUMS
```

Or verify a single artifact:

```bash
sha256sum keymasq_*_all.deb
grep 'keymasq_.*_all.deb' SHA256SUMS
```
