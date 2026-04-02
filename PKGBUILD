# Maintainer: nyrda <nyrda@keyforge.tools>
pkgname=keyforge
pkgver=0.1.0
pkgrel=1
pkgdesc="A key remapping tool for Linux using evdev and uinput"
arch=('any')
url="https://github.com/nyrda/keyforge"
license=('MIT')
depends=(
    'acl'
    'slurp'
    'python>=3.12'
    'python-evdev>=1.6.0'
    'python-tomli-w>=1.0.0'
    'python-dbus-next>=0.2.3'
    'python-xlib>=0.33'
    'python-gobject>=3.42.0'
    'gtk4'
    'libadwaita'
    'polkit'
    'systemd'
)
makedepends=(
    'python-setuptools>=61.0'
    'python-wheel'
    'python-build'
    'python-installer'
    'git'
)
optdepends=(
    'hyprland: for Hyprland window rule support'
)
install="$pkgname.install"
source=(
    "file:///home/daniel/dev/keyforge/dist/keyforge-$pkgver.tar.gz"
)
sha256sums=('733df91f824cdb12bafbe9cdd3b0b4e05af918663330d826ba45438ef629b4aa')

build() {
    cd "$pkgname-$pkgver"
    python -m build --wheel --no-isolation
}

package() {
    cd "$pkgname-$pkgver"

    python -m installer --destdir="$pkgdir" dist/*.whl

    install -Dm644 "assets/keyforge.desktop" \
        "$pkgdir/usr/share/applications/keyforge.desktop"
    install -Dm644 "assets/keyforge.metainfo.xml" \
        "$pkgdir/usr/share/metainfo/keyforge.metainfo.xml"
    install -Dm644 "assets/keyforge.svg" \
        "$pkgdir/usr/share/icons/hicolor/scalable/apps/keyforge.svg"

    install -Dm644 "systemd/keyforged.service" \
        "$pkgdir/usr/lib/systemd/system/keyforged.service"
    install -Dm644 "systemd/keyforge-session.service" \
        "$pkgdir/usr/lib/systemd/user/keyforge-session.service"

    install -Dm644 "sysusers.d/keyforge.conf" \
        "$pkgdir/usr/lib/sysusers.d/keyforge.conf"

    install -Dm644 "tmpfiles.d/keyforge.conf" \
        "$pkgdir/usr/lib/tmpfiles.d/keyforge.conf"

    install -Dm644 "udev/91-keyforge-acl.rules" \
        "$pkgdir/usr/lib/udev/rules.d/91-keyforge-acl.rules"

    install -Dm644 "polkit/com.keyforge.record-macro.policy" \
        "$pkgdir/usr/share/polkit-1/actions/com.keyforge.record-macro.policy"

    install -Dm644 "LICENSE" "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
    install -Dm644 "README.md" "$pkgdir/usr/share/doc/$pkgname/README.md"
    install -Dm644 "examples/security.toml" "$pkgdir/usr/share/doc/$pkgname/security.toml"

    rm -f "$pkgdir/usr/bin/keyforge-cli" "$pkgdir/usr/bin/keyforge-gui"
}
