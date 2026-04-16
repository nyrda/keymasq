# Debian Packaging Layout

Debian packaging is split into two layers:

- `debian/` contains the native Debian source package metadata.
- The actual package payload stays in the existing top-level asset directories:
  `systemd/`, `udev/`, `sysusers.d/`, `tmpfiles.d/`, `polkit/`, `assets/`,
  `examples/`, and `gnome-extension/`.

This keeps Debian-specific metadata separate from the reusable package contents.

## Build

```bash
dpkg-buildpackage -us -uc -b
```

The resulting package is written to the parent directory, for example:

```bash
../keymasq_0.1.0-1_all.deb
```

Verify the package metadata before installation:

```bash
dpkg-deb -I ../keymasq_0.1.0-1_all.deb
dpkg-deb -c ../keymasq_0.1.0-1_all.deb
lintian ../keymasq_0.1.0-1_all.deb
```

## Autopkgtest

Native autopkgtest definitions live in `debian/tests/`.

For a local QEMU/KVM run:

```bash
autopkgtest . -- qemu /path/to/debian-autopkgtest.qcow2
```

For a local libvirt VM disk, use the helper below to resolve the backing image
from a `virsh` domain and run autopkgtest against a temporary qcow2 overlay:

```bash
packaging/debian/run-autopkgtest-libvirt.sh --domain debian-trixie
```

For the local libvirt/ZFS flow used by this repo, create the VM once:

```bash
packaging/debian/create-libvirt-autopkgtest-vm.sh \
  --name debian-trixie-autopkgtest \
  --pool pool \
  --release trixie
```

Then run the tests against the existing domain:

```bash
packaging/debian/run-autopkgtest-libvirt.sh \
  --domain debian-trixie-autopkgtest \
  --user autopkgtest \
  --password '<vm-password>'
```

If you need to debug inside the guest, build and install the package first and
run the test scripts directly:

```bash
dpkg-buildpackage -us -uc -b
sudo apt-get install -y ../keymasq_0.1.0-1_all.deb
sudo sh debian/tests/pkg-smoke
sh debian/tests/installed-cli
```
