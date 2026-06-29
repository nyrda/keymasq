#!/usr/bin/env bash
set -euo pipefail

pacman -Syu --noconfirm --needed \
  acl \
  base-devel \
  ca-certificates \
  curl \
  file \
  gobject-introspection \
  gtk4 \
  hicolor-icon-theme \
  libarchive \
  libadwaita \
  librsvg \
  openssl \
  patchelf \
  python \
  python-build \
  python-cairo \
  python-dbus-next \
  python-evdev \
  python-gobject \
  python-installer \
  python-setuptools \
  python-tomli-w \
  python-uvloop \
  python-wheel \
  python-xlib \
  rsync \
  slurp \
  squashfs-tools \
  udev \
  waypipe \
  wget \
  xkeyboard-config
