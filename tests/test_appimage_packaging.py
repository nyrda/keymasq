import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = ROOT / "scripts/check.sh"
CI_WORKFLOW = ROOT / ".github/workflows/tests.yml"
RUNTIME_SCRIPT = ROOT / "packaging/appimage/runtime/keymasq-appimage-runtime.sh"
VERIFY_SCRIPT = ROOT / "packaging/appimage/verify-appimage.sh"
APPIMAGE_ASSETS = ROOT / "packaging/appimage/assets"
APPIMAGE_BUILDER = ROOT / "packaging/appimage/make-appimage.sh"
BROTWAY_LAUNCHER = ROOT / "packaging/appimage/runtime/gtk4-brotway-run.sh"
BROTWAY_DEBUGMENU_LAUNCHER = ROOT / "packaging/appimage/runtime/gtk4-brotway-debugmenu.sh"
BROTWAY_TEST_RUNNER = ROOT / "scripts/test-appimage-brotway"
GLIBC_COMPATIBILITY_CHECK = ROOT / "packaging/appimage/check-glibc-compatibility.sh"
ICON_GALLERY_RUNNER = ROOT / "nix/appimage-brotway-integration-test/run_icon_gallery.sh"
PYTHON_RUNTIME_PACKAGE_MANIFEST = APPIMAGE_ASSETS / "python-runtime-site-packages.txt"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_appimage_builder_pins_the_brotway_release() -> None:
    builder = APPIMAGE_BUILDER.read_text(encoding="utf-8")

    version_match = re.search(
        r'BROTWAY_BUNDLE_VERSION="\$\{KEYMASQ_APPIMAGE_BROTWAY_BUNDLE_VERSION:-([^}]+)\}"',
        builder,
    )
    assert version_match is not None
    assert version_match.group(1) not in {"latest", "main", "master"}
    assert "KEYMASQ_APPIMAGE_BROTWAY_BUNDLE_NAME:-" in builder
    assert "github.com/nyrda/gtk-brotway/releases/download" in builder
    checksum_match = re.search(
        r'BROTWAY_BUNDLE_SHA256="\$\{KEYMASQ_APPIMAGE_BROTWAY_BUNDLE_SHA256:-([0-9a-f]{64})\}"',
        builder,
    )
    assert checksum_match is not None
    checksum_guard = 'if [[ -z "$BROTWAY_BUNDLE_SHA256" ]]'
    local_bundle_branch = 'if [[ -n "$local_bundle" ]]'
    assert builder.index(checksum_guard) < builder.index(local_bundle_branch)
    assert 'verify_sha256 "gtk-brotway bundle" "$local_bundle"' in builder
    assert "command -v bsdtar" in builder


def test_appimage_dependency_scan_limits_file_probes() -> None:
    builder = APPIMAGE_BUILDER.read_text(encoding="utf-8")

    assert "-name '*.so' -o -name '*.so.*' -o -perm /111" in builder
    assert 'find "$root" -type f -print0' not in builder


def test_all_appimage_changes_select_the_full_ci_and_local_gates() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    check_script = CHECK_SCRIPT.read_text(encoding="utf-8")

    assert '"packaging/appimage/**"' in workflow
    auto_category = check_script.split("resolve_auto_category() {", maxsplit=1)[1].split(
        "\n}", maxsplit=1
    )[0]
    assert auto_category.count("packaging/appimage") == 3
    assert "packaging/appimage/*)" in auto_category


def test_icon_gallery_runner_defaults_to_its_dedicated_port() -> None:
    runner = ICON_GALLERY_RUNNER.read_text(encoding="utf-8")

    assert "port=${KEYMASQ_ICON_GALLERY_PORT:-18102}" in runner


def test_appimage_checks_brotway_against_bundled_glibc_before_dependency_scan() -> None:
    builder = APPIMAGE_BUILDER.read_text(encoding="utf-8")

    install = "install_brotway_runtime\n"
    compatibility = "verify_brotway_glibc_compatibility\n"
    dependency_scan = 'bundle_elf_dependencies "$python_lib_dir"'
    assert builder.index(install) < builder.index(compatibility)
    assert builder.index(compatibility) < builder.index(dependency_scan)
    assert "check-glibc-compatibility.sh" in builder
    assert '"$APPDIR/lib/gtk4-brotway/libgtk-4.so.1"' in builder
    assert '"$APPDIR/lib/libc.so.6"' in builder


def test_glibc_compatibility_check_accepts_and_rejects_symbol_version_sets(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "readelf",
        '#!/bin/sh\nfor path do :; done\nexec cat "$path"\n',
    )
    consumer = tmp_path / "libgtk-4.so.1"
    consumer.write_text(
        """Version needs section '.gnu.version_r' contains 1 entry:
  000000: Version: 1  File: libc.so.6  Cnt: 2
  0x0010:   Name: GLIBC_2.34  Flags: none  Version: 3
  0x0020:   Name: GLIBC_2.38  Flags: none  Version: 2
""",
        encoding="utf-8",
    )
    libc = tmp_path / "libc.so.6"
    libc.write_text(
        """Version definition section '.gnu.version_d' contains 2 entries:
  000000: Rev: 1  Flags: none  Index: 2  Cnt: 1  Name: GLIBC_2.34
  0x001c: Rev: 1  Flags: none  Index: 3  Cnt: 1  Name: GLIBC_2.38
""",
        encoding="utf-8",
    )
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}

    compatible = subprocess.run(
        ["bash", str(GLIBC_COMPATIBILITY_CHECK), str(consumer), str(libc)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert compatible.returncode == 0
    assert "2 required symbol versions are available" in compatible.stdout

    libc.write_text(
        """Version definition section '.gnu.version_d' contains 1 entry:
  000000: Rev: 1  Flags: none  Index: 2  Cnt: 1  Name: GLIBC_2.34
""",
        encoding="utf-8",
    )
    incompatible = subprocess.run(
        ["bash", str(GLIBC_COMPATIBILITY_CHECK), str(consumer), str(libc)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert incompatible.returncode == 1
    assert "Brotway GTK is incompatible with the AppImage libc" in incompatible.stderr
    assert "missing GLIBC symbol versions:\n  GLIBC_2.38\n" in incompatible.stderr


def test_appimage_builder_copies_only_the_python_runtime_closure() -> None:
    builder = APPIMAGE_BUILDER.read_text(encoding="utf-8")
    packages = frozenset(PYTHON_RUNTIME_PACKAGE_MANIFEST.read_text(encoding="utf-8").splitlines())

    assert packages == {
        "PyGObject-*.dist-info",
        "Xlib",
        "cairo",
        "dbus_next",
        "dbus_next-*.dist-info",
        "evdev",
        "evdev-*.dist-info",
        "gi",
        "pycairo-*.dist-info",
        "python_xlib-*.dist-info",
        "six-*.dist-info",
        "six.py",
        "tomli_w",
        "tomli_w-*.dist-info",
        "uvloop",
        "uvloop-*.dist-info",
    }
    copy_function = builder.split("copy_python_packages() {", maxsplit=1)[1].split(
        "\n}", maxsplit=1
    )[0]
    assert "python-runtime-site-packages.txt" in copy_function
    assert "rsync" not in copy_function
    assert 'rm -rf "$bundled_site"' in copy_function
    assert 'cp -aL "$source"' in copy_function


def test_appimage_builder_installs_a_private_raster_icon_theme() -> None:
    builder = APPIMAGE_BUILDER.read_text(encoding="utf-8")

    assert "install_appimage_icons" in builder
    assert "encode-symbolic-icon.py" in builder
    assert "keymasq-icon-theme.index.theme" in builder
    assert 'rm -f "$package_assets"/*-symbolic.svg "$package_assets/gamepad.svg"' in builder
    assert "gtk-update-icon-cache --force" in builder


def test_appimage_builder_makes_the_installed_payload_world_readable() -> None:
    builder = APPIMAGE_BUILDER.read_text(encoding="utf-8")

    permissions = 'chmod -R a+rX "$APPDIR"'
    assert permissions in builder
    assert builder.index(permissions) < builder.index('"$quick_sharun" --make-appimage')


def test_brotway_launcher_keeps_child_processes_in_the_appimage_runtime() -> None:
    launcher = BROTWAY_LAUNCHER.read_text(encoding="utf-8")

    assert 'export BROTWAY_LOADER="$loader"' in launcher
    assert 'export BROTWAY_LIBRARY_PATH="$library_path"' in launcher
    assert 'export BROTWAY_HELPER_PATH="$appdir/bin"' in launcher
    assert "brotway_address=${BROTWAY_ADDRESS:-127.0.0.1}" in launcher
    assert 'export BROTWAY_ADDRESS="$brotway_address"' in launcher
    assert 'export BROTWAY_DISPLAY="$brotway_display"' in launcher
    assert "export KEYMASQ_APPIMAGE_RENDERING=auto" in launcher
    assert "export GSK_RENDERER=broadway" in launcher
    assert 'export DISPLAY="$brotway_display"' in launcher
    assert "KEYMASQ_GUI_NON_UNIQUE" not in launcher
    assert "export LD_LIBRARY_PATH=" not in launcher


def test_brotway_debugmenu_uses_the_loader_without_exporting_library_path() -> None:
    launcher = BROTWAY_DEBUGMENU_LAUNCHER.read_text(encoding="utf-8")

    assert 'exec "$loader" --library-path "$library_path"' in launcher
    assert "export LD_LIBRARY_PATH=" not in launcher


def test_brotway_launcher_does_not_expose_appimage_libraries_to_host_shells(
    tmp_path: Path,
) -> None:
    appdir = tmp_path / "AppDir"
    brotway_dir = appdir / "lib/gtk4-brotway"
    brotway_dir.mkdir(parents=True)
    (appdir / "bin").mkdir()
    (appdir / "shared/bin").mkdir(parents=True)
    log_path = tmp_path / "launcher-env.log"
    _write_executable(brotway_dir / "gtk4-brotway-run", "#!/bin/sh\nexit 0\n")
    _write_executable(appdir / "shared/bin/python3", "#!/bin/sh\nexit 0\n")
    _write_executable(
        appdir / "lib/ld-linux-x86-64.so.2",
        """#!/bin/sh
printf 'LD_LIBRARY_PATH=%s\n' "${LD_LIBRARY_PATH-unset}" > "$KEYMASQ_LAUNCHER_ENV_LOG"
printf 'BROTWAY_LIBRARY_PATH=%s\n' "$BROTWAY_LIBRARY_PATH" >> "$KEYMASQ_LAUNCHER_ENV_LOG"
printf 'BROTWAY_ADDRESS=%s\n' "$BROTWAY_ADDRESS" >> "$KEYMASQ_LAUNCHER_ENV_LOG"
printf 'BROTWAY_DISPLAY=%s\n' "$BROTWAY_DISPLAY" >> "$KEYMASQ_LAUNCHER_ENV_LOG"
printf 'DISPLAY=%s\n' "$DISPLAY" >> "$KEYMASQ_LAUNCHER_ENV_LOG"
printf 'argv=%s\n' "$*" >> "$KEYMASQ_LAUNCHER_ENV_LOG"
""",
    )
    env = os.environ.copy()
    env.update(
        {
            "APPDIR": str(appdir),
            "KEYMASQ_LAUNCHER_ENV_LOG": str(log_path),
            "LD_LIBRARY_PATH": "/host/libraries",
            "BROTWAY_DISPLAY": ":8",
            "DISPLAY": ":99",
        }
    )
    env.pop("BROTWAY_ADDRESS", None)

    subprocess.run(
        [
            "sh",
            str(BROTWAY_LAUNCHER),
            "--display",
            ":09",
            "/opt/keymasq/bin/keymasq",
        ],
        check=True,
        env=env,
    )

    launcher_env = log_path.read_text(encoding="utf-8")
    library_path = f"{brotway_dir}:{appdir}/lib"
    assert "LD_LIBRARY_PATH=/host/libraries\n" in launcher_env
    assert f"BROTWAY_LIBRARY_PATH={library_path}\n" in launcher_env
    assert "BROTWAY_ADDRESS=127.0.0.1\n" in launcher_env
    assert "BROTWAY_DISPLAY=:09\n" in launcher_env
    assert "DISPLAY=:09\n" in launcher_env
    assert f"argv=--library-path {library_path} " in launcher_env
    assert launcher_env.endswith(" --display :09 /opt/keymasq/bin/keymasq\n")


def test_brotway_launcher_rejects_an_occupied_port_before_starting_gtk(
    tmp_path: Path,
) -> None:
    appdir = tmp_path / "AppDir"
    brotway_dir = appdir / "lib/gtk4-brotway"
    brotway_dir.mkdir(parents=True)
    (appdir / "bin").mkdir()
    (appdir / "shared/bin").mkdir(parents=True)
    started = tmp_path / "gtk-started"
    _write_executable(
        brotway_dir / "gtk4-brotway-run",
        f"#!/bin/sh\ntouch {started}\n",
    )
    _write_executable(
        appdir / "lib/ld-linux-x86-64.so.2",
        '#!/bin/sh\nshift 2\nexec "$@"\n',
    )
    (appdir / "shared/bin/python3").symlink_to(sys.executable)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        result = subprocess.run(
            [
                "sh",
                str(BROTWAY_LAUNCHER),
                "--address",
                "127.0.0.1",
                "--port",
                str(port),
                "/opt/keymasq/bin/keymasq",
            ],
            env={**os.environ, "APPDIR": str(appdir)},
            check=False,
            capture_output=True,
            text=True,
        )

    assert result.returncode == 1
    assert result.stderr == (f"gtk4-brotway-run: port {port} is already in use on 127.0.0.1\n")
    assert not started.exists()


def test_brotway_launcher_allows_an_immediate_server_restart(tmp_path: Path) -> None:
    appdir = tmp_path / "AppDir"
    brotway_dir = appdir / "lib/gtk4-brotway"
    brotway_dir.mkdir(parents=True)
    (appdir / "bin").mkdir()
    (appdir / "shared/bin").mkdir(parents=True)
    started = tmp_path / "gtk-started"
    _write_executable(
        brotway_dir / "gtk4-brotway-run",
        f"#!/bin/sh\ntouch {started}\n",
    )
    _write_executable(
        appdir / "lib/ld-linux-x86-64.so.2",
        '#!/bin/sh\nshift 2\nexec "$@"\n',
    )
    (appdir / "shared/bin/python3").symlink_to(sys.executable)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port)) as client:
            connection, _ = listener.accept()
            connection.close()
            assert client.recv(1) == b""

    subprocess.run(
        [
            "sh",
            str(BROTWAY_LAUNCHER),
            "--address",
            "127.0.0.1",
            "--port",
            str(port),
            "/opt/keymasq/bin/keymasq",
        ],
        env={**os.environ, "APPDIR": str(appdir)},
        check=True,
    )

    assert started.exists()


def _run_brotway_test_runner(
    tmp_path: Path,
    *,
    path_info_status: int,
) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
    appimage = tmp_path / "Keymasq.AppImage"
    _write_executable(appimage, "fake AppImage\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "nix-calls.log"
    _write_executable(
        bin_dir / "nix",
        """#!/bin/sh
printf 'artifact=%s args=%s\n' "$KEYMASQ_APPIMAGE_TEST_ARTIFACT" "$*" >> "$KEYMASQ_TEST_NIX_LOG"
if [ "${1:-}" = path-info ]; then
  exit "$KEYMASQ_TEST_PATH_INFO_STATUS"
fi
exit 0
""",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "KEYMASQ_TEST_NIX_LOG": str(log_path),
            "KEYMASQ_TEST_PATH_INFO_STATUS": str(path_info_status),
        }
    )
    result = subprocess.run(
        ["bash", str(BROTWAY_TEST_RUNNER), str(appimage)],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, log_path.read_text(encoding="utf-8").splitlines(), appimage


def test_brotway_test_runner_builds_a_new_artifact_without_a_store_precopy(
    tmp_path: Path,
) -> None:
    result, calls, appimage = _run_brotway_test_runner(
        tmp_path,
        path_info_status=1,
    )

    assert result.returncode == 0, result.stderr
    assert len(calls) == 2
    assert f"artifact={appimage}" in calls[0]
    assert "args=path-info --impure" in calls[0]
    assert "args=build --no-link --impure" in calls[1]
    assert "--rebuild" not in calls[1]
    assert all("store add-path" not in call for call in calls)


def test_brotway_test_runner_rebuilds_an_existing_result(tmp_path: Path) -> None:
    result, calls, appimage = _run_brotway_test_runner(
        tmp_path,
        path_info_status=0,
    )

    assert result.returncode == 0, result.stderr
    assert len(calls) == 2
    assert f"artifact={appimage}" in calls[1]
    assert "args=build --no-link --rebuild --impure" in calls[1]


def _fake_command_dir(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "commands.log"
    fake = """#!/bin/sh
printf '%s %s\\n' "${0##*/}" "$*" >> "$KEYMASQ_COMMAND_LOG"
name=${0##*/}
if [ -n "${KEYMASQ_COMMAND_ENV_LOG:-}" ]; then
  {
    printf '%s PYTHONHOME=%s\\n' "$name" "${PYTHONHOME-unset}"
    printf '%s LD_LIBRARY_PATH=%s\\n' "$name" "${LD_LIBRARY_PATH-unset}"
    printf '%s PYTHONPATH=%s\\n' "$name" "${PYTHONPATH-unset}"
  } >> "$KEYMASQ_COMMAND_ENV_LOG"
fi
if [ "$name" = chown ] && [ "${KEYMASQ_FAKE_CHOWN_STATUS:-0}" != 0 ]; then
  exit "$KEYMASQ_FAKE_CHOWN_STATUS"
fi
if [ "$name" = udevadm ] && [ "${1:-}" = control ]; then
  if [ "${KEYMASQ_FAKE_UDEVADM_CONTROL_STATUS:-0}" != 0 ]; then
    exit "$KEYMASQ_FAKE_UDEVADM_CONTROL_STATUS"
  fi
fi
if [ "$name" = systemctl ] && [ "${1:-}" = is-active ]; then
  exit "${KEYMASQ_FAKE_SYSTEMD_ACTIVE_STATUS:-3}"
fi
if [ "$name" = systemctl ] && [ "${1:-}" = is-enabled ]; then
  exit "${KEYMASQ_FAKE_SYSTEMD_ENABLED_STATUS:-1}"
fi
if [ "$name" = systemctl ] && [ "${1:-}" = try-restart ]; then
  exit "${KEYMASQ_FAKE_SYSTEMD_RESTART_STATUS:-0}"
fi
if [ "$name" = runuser ]; then
  case " $* " in
    *" systemctl --user is-active --quiet keymasq-session.service "*)
      exit "${KEYMASQ_FAKE_USER_SYSTEMD_ACTIVE_STATUS:-3}"
      ;;
    *" systemctl --user is-enabled --quiet keymasq-session.service "*)
      exit "${KEYMASQ_FAKE_USER_SYSTEMD_ENABLED_STATUS:-1}"
      ;;
    *" systemctl --user try-restart keymasq-session.service "*)
      exit "${KEYMASQ_FAKE_USER_SYSTEMD_RESTART_STATUS:-0}"
      ;;
  esac

  case " $* " in
    *" systemctl --user "*)
      exit 0
      ;;
  esac

  while [ "$#" -gt 0 ] && [ "$1" != -- ]; do
    shift
  done
  if [ "${1:-}" = -- ]; then
    shift
  fi
  exec "$@" </dev/null
fi
exit 0
"""
    for name in (
        "chown",
        "systemd-sysusers",
        "systemd-tmpfiles",
        "udevadm",
        "setfacl",
        "systemctl",
        "runuser",
    ):
        _write_executable(bin_dir / name, fake)
    return bin_dir, log_path


def _asset_dir(tmp_path: Path) -> Path:
    assets = tmp_path / "assets"
    shutil.copytree(APPIMAGE_ASSETS, assets)
    shutil.copy2(ROOT / "udev/91-keymasq-acl.rules", assets / "91-keymasq-acl.rules")
    shutil.copy2(
        ROOT / "udev/99-keymasq-hide-grabbed.rules",
        assets / "99-keymasq-hide-grabbed.rules",
    )
    shutil.copy2(
        ROOT / "assets/tools.keymasq.keymasq.svg",
        assets / "tools.keymasq.keymasq.svg",
    )
    return assets


def _fake_extracted_appdir(tmp_path: Path, assets: Path) -> Path:
    appdir = tmp_path / "fake-appdir"
    bin_dir = appdir / "bin"
    bin_dir.mkdir(parents=True)
    for name in (
        "keymasq",
        "keymasqd",
        "keymasq-session",
        "keymasq-record",
        "slurp",
        "waypipe",
        "gtk4-brotway-run",
    ):
        _write_executable(bin_dir / name, f"#!/bin/sh\nprintf '%s\\n' {name}\n")
    brotway_dir = appdir / "lib/gtk4-brotway"
    brotway_dir.mkdir(parents=True)
    for name in (
        "gtk4-broadwayd",
        "gtk4-brotway-run",
        "gtk4-brotway-debugmenu",
        "libgtk-4.so.1",
    ):
        _write_executable(brotway_dir / name, "#!/bin/sh\nexit 0\n")
    shutil.copytree(assets, appdir / "share/keymasq/appimage")
    return appdir


def _env(tmp_path: Path, fake_root: Path, assets: Path, source_appimage: Path) -> dict[str, str]:
    fake_bin, command_log = _fake_command_dir(tmp_path)
    fake_appdir = _fake_extracted_appdir(tmp_path, assets)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "KEYMASQ_APPIMAGE_ROOT": str(fake_root),
            "KEYMASQ_APPIMAGE_SKIP_PRIVILEGE_CHECK": "1",
            "KEYMASQ_APPIMAGE_ASSET_DIR": str(assets),
            "KEYMASQ_APPIMAGE_EXTRACTED_SOURCE_DIR": str(fake_appdir),
            "KEYMASQ_APPIMAGE_SOURCE": str(source_appimage),
            "KEYMASQ_COMMAND_LOG": str(command_log),
            "APPIMAGE": str(source_appimage),
        }
    )
    return env


def _write_update_manifest(
    update_dir: Path,
    appimage: Path,
    version: str,
    architecture: str = "x86_64",
) -> str:
    sha256 = subprocess.check_output(["sha256sum", str(appimage)], text=True).split()[0]
    manifest = update_dir / "latest-x86_64.json"
    manifest.write_text(
        json.dumps(
            {
                "version": version,
                "architecture": architecture,
                "appimage_url": appimage.as_uri(),
                "sha256": sha256,
            }
        ),
        encoding="utf-8",
    )
    return sha256


def _install_fake_gpg(env: dict[str, str]) -> None:
    fake_bin = Path(env["PATH"].split(":", 1)[0])
    _write_executable(
        fake_bin / "gpg",
        """#!/bin/sh
printf '%s %s\\n' "${0##*/}" "$*" >> "$KEYMASQ_COMMAND_LOG"
case " $* " in
  *" --import "*)
    exit 0
    ;;
  *" --verify "*)
    signature=$(printf '%s\\n' "$@" | tail -n 2 | head -n 1)
    [ "$(cat "$signature")" = "trusted-signature" ] || exit 1
    exit 0
    ;;
esac
exit 1
""",
    )


def _fake_verify_appimage(path: Path) -> None:
    _write_executable(
        path,
        """#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
  --help)
    exit 0
    ;;
  keymasq|keymasq-record)
    if [[ "${2:-}" = "--help" ]]; then
      exit 0
    fi
    ;;
  --appimage-extract)
    root="${KEYMASQ_FAKE_EXTRACT_ROOT_NAME:-squashfs-root}"
    mkdir -p "$root/shared/bin" "$root/lib" "$root/bin"
    printf '#!/bin/sh\\nexit 99\\n' > "$root/shared/bin/python3.12"
    chmod 0755 "$root/shared/bin/python3.12"
    printf '#!/bin/sh\\nexit 0\\n' > "$root/bin/slurp"
    chmod 0755 "$root/bin/slurp"
    printf '#!/bin/sh\\nexit 0\\n' > "$root/bin/waypipe"
    chmod 0755 "$root/bin/waypipe"
    printf '#!/bin/sh\\nexit 0\\n' > "$root/bin/gtk4-brotway-run"
    chmod 0755 "$root/bin/gtk4-brotway-run"
    mkdir -p \
      "$root/lib/gtk4-brotway" \
      "$root/lib/python3.12/site-packages" \
      "$root/share/doc/keymasq" \
      "$root/share/icons/Keymasq" \
      "$root/share/keymasq/appimage"
    for name in gtk4-broadwayd gtk4-brotway-run gtk4-brotway-debugmenu libgtk-4.so.1; do
      printf '#!/bin/sh\\nexit 0\\n' > "$root/lib/gtk4-brotway/$name"
      chmod 0755 "$root/lib/gtk4-brotway/$name"
    done
    printf '{}\\n' > "$root/share/doc/keymasq/gtk4-brotway-manifest.json"
    printf '[Icon Theme]\\nName=Keymasq\\n' > "$root/share/icons/Keymasq/index.theme"
    printf 'list-add-symbolic\\n' > "$root/share/keymasq/appimage/gui-icon-names.txt"
    printf 'gi\\n' > "$root/share/keymasq/appimage/python-runtime-site-packages.txt"
    cat > "$root/lib/ld-linux-x86-64.so.2" <<'LOADER'
#!/bin/sh
printf '%s\\n' "$APPDIR" > "$KEYMASQ_FAKE_VERIFY_APPDIR_LOG"
case "$APPDIR" in
  */"$KEYMASQ_FAKE_EXTRACT_ROOT_NAME") exit 0 ;;
  *) exit 88 ;;
esac
LOADER
    chmod 0755 "$root/lib/ld-linux-x86-64.so.2"
    exit 0
    ;;
esac

exit 1
""",
    )


def test_appimage_verifier_accepts_standard_extraction_roots(tmp_path: Path) -> None:
    appimage = tmp_path / "Keymasq.AppImage"
    _fake_verify_appimage(appimage)

    for root_name in ("AppDir", "squashfs-root"):
        log_path = tmp_path / f"{root_name}.log"
        env = os.environ.copy()
        env.update(
            {
                "KEYMASQ_FAKE_EXTRACT_ROOT_NAME": root_name,
                "KEYMASQ_FAKE_VERIFY_APPDIR_LOG": str(log_path),
            }
        )

        subprocess.run(["bash", str(VERIFY_SCRIPT), str(appimage)], check=True, env=env)

        assert log_path.read_text(encoding="utf-8").rstrip().endswith(f"/{root_name}")


def test_appimage_privilege_escalation_uses_installed_root_appimage(
    tmp_path: Path,
) -> None:
    fake_root = tmp_path / "root"
    installed_appimage = fake_root / "opt/keymasq/Keymasq.AppImage"
    installed_appimage.parent.mkdir(parents=True)
    _write_executable(installed_appimage, "#!/bin/sh\nexit 99\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    pkexec_log = tmp_path / "pkexec.log"
    _write_executable(
        fake_bin / "id",
        """#!/bin/sh
if [ "${1:-}" = -u ]; then
  printf '1000\\n'
  exit 0
fi
exit 1
""",
    )
    _write_executable(
        fake_bin / "pkexec",
        """#!/bin/sh
printf '%s\\n' "$*" > "$KEYMASQ_PKEXEC_LOG"
exit 0
""",
    )
    _write_executable(fake_bin / "keymasq", "#!/bin/sh\nexit 88\n")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "KEYMASQ_APPIMAGE_ROOT": str(fake_root),
            "KEYMASQ_PKEXEC_LOG": str(pkexec_log),
        }
    )
    env.pop("APPIMAGE", None)
    env.pop("KEYMASQ_APPIMAGE_SKIP_PRIVILEGE_CHECK", None)

    subprocess.run(
        ["sh", str(RUNTIME_SCRIPT), "--self-update", "--user", "root"],
        check=True,
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert pkexec_log.read_text(encoding="utf-8") == (
        f"{installed_appimage} --self-update --user root\n"
    )


def test_appimage_builder_does_not_force_software_rendering_by_default() -> None:
    builder = (ROOT / "packaging/appimage/make-appimage.sh").read_text(encoding="utf-8")

    assert "KEYMASQ_APPIMAGE_BUNDLE_OPENGL" not in builder
    assert "KEYMASQ_APPIMAGE_BUNDLE_VULKAN" not in builder
    assert "export DEPLOY_OPENGL=0" in builder
    assert "export DEPLOY_VULKAN=0" in builder
    assert "remove_bundled_graphics_drivers" in builder
    graphics_cleanup = builder.split("remove_bundled_graphics_drivers() {", 1)[1].split("\n}", 1)[0]
    assert "libEGL.so\\*" not in graphics_cleanup
    assert "libvulkan.so\\*" not in graphics_cleanup
    assert '"$APPDIR/lib/dri"' in graphics_cleanup
    assert '"$APPDIR/share/vulkan"' in graphics_cleanup
    assert "KEYMASQ_APPIMAGE_ALWAYS_SOFTWARE" in builder
    assert "ALWAYS_SOFTWARE:-0" in builder
    assert "ALWAYS_SOFTWARE:-1" not in builder


def test_appimage_builder_verifies_transitive_inputs() -> None:
    builder = (ROOT / "packaging/appimage/make-appimage.sh").read_text(encoding="utf-8")

    assert "prepare_verified_appimage_inputs" in builder
    assert "ANYLINUX_SOURCE_SHA256" in builder
    assert "KEYMASQ_APPIMAGE_SHARUN_SHA256" in builder
    assert "KEYMASQ_APPIMAGE_APPIMAGETOOL_SHA256" in builder
    assert "KEYMASQ_APPIMAGE_URUNTIME_SHA256" in builder
    assert "KEYMASQ_APPIMAGE_DWARFS_SHA256" in builder
    assert 'RUNTIME="$uruntime"' in builder
    assert 'DWARFS_CMD="$mkdwarfs"' in builder
    assert "/releases/latest/" not in builder
    assert "refs/heads/main" not in builder
    assert "export ADD_HOOKS=" in builder
    assert "export GTK_CLASS_FIX=0" in builder
    assert "export OPTIMIZE_LAUNCH=0" in builder
    assert "export PATH_MAPPING=" in builder
    assert "remove_generated_hardcoded_path_mapping" in builder
    assert 'hook="$APPDIR/bin/01-path-mapping-hardcoded.hook"' in builder
    assert "hook.unlink()" in builder


def test_appimage_installer_writes_steamos_integration(tmp_path: Path) -> None:
    fake_root = tmp_path / "root"
    (fake_root / "etc").mkdir(parents=True)
    (fake_root / "etc/os-release").write_text("ID=steamos\n", encoding="utf-8")
    assets = _asset_dir(tmp_path)
    source_appimage = tmp_path / "Keymasq.AppImage"
    source_appimage.write_text("appimage\n", encoding="utf-8")
    source_appimage.chmod(0o755)
    env = _env(tmp_path, fake_root, assets, source_appimage)
    env["KEYMASQ_APPIMAGE_SERVICE_MANAGER"] = "systemd"

    subprocess.run(
        ["sh", str(RUNTIME_SCRIPT), "--install", "--user", "root"],
        check=True,
        env=env,
    )

    assert (fake_root / "opt/keymasq/Keymasq.AppImage").read_text(encoding="utf-8") == (
        "appimage\n"
    )
    sha256 = subprocess.check_output(["sha256sum", str(source_appimage)], text=True).split()[0]
    runtime_dir = fake_root / f"opt/keymasq/runtime/{sha256}"
    assert (fake_root / "opt/keymasq/runtime/current").readlink() == Path(sha256)
    assert (runtime_dir / "bin/keymasq").is_file()
    assert (runtime_dir / "bin/keymasqd").is_file()
    assert (runtime_dir / "bin/slurp").is_file()
    assert (runtime_dir / "bin/waypipe").is_file()
    assert (runtime_dir / "bin/gtk4-brotway-run").is_file()
    assert (runtime_dir / "lib/gtk4-brotway/gtk4-broadwayd").is_file()
    assert (runtime_dir / "lib/gtk4-brotway/gtk4-brotway-run").is_file()
    assert (runtime_dir / "lib/gtk4-brotway/gtk4-brotway-debugmenu").is_file()
    assert 'exec "$APPDIR/bin/waypipe" "$@"' in (fake_root / "opt/keymasq/bin/waypipe").read_text(
        encoding="utf-8"
    )
    assert 'exec "$APPDIR/bin/gtk4-brotway-run" "$@"' in (
        fake_root / "opt/keymasq/bin/gtk4-brotway-run"
    ).read_text(encoding="utf-8")
    assert "APPDIR=${KEYMASQ_APPDIR:-/opt/keymasq/runtime/current}" in (
        fake_root / "opt/keymasq/bin/keymasqd"
    ).read_text(encoding="utf-8")
    assert 'exec "$APPDIR/bin/keymasqd" "$@"' in (fake_root / "opt/keymasq/bin/keymasqd").read_text(
        encoding="utf-8"
    )
    assert 'exec "$APPDIR/bin/keymasq" "$@"' in (fake_root / "opt/keymasq/bin/keymasq").read_text(
        encoding="utf-8"
    )
    assert (fake_root / "root/.local/bin/keymasq").is_file()
    assert "/opt/keymasq/bin:$PATH" in (fake_root / "etc/profile.d/keymasq.sh").read_text(
        encoding="utf-8"
    )
    keymasqd_unit = (fake_root / "etc/systemd/system/keymasqd.service").read_text(encoding="utf-8")
    assert "ExecStart=/opt/keymasq/bin/keymasqd" in keymasqd_unit
    assert "NotifyAccess=all" in keymasqd_unit
    assert "APPIMAGE_EXTRACT_AND_RUN" not in keymasqd_unit
    assert "KillMode=mixed" in keymasqd_unit
    assert "ProtectSystem=strict" in keymasqd_unit
    assert "ReadWritePaths=/run/keymasq /var/lib/keymasq" in keymasqd_unit
    assert "/opt/keymasq" not in next(
        line for line in keymasqd_unit.splitlines() if line.startswith("ReadWritePaths=")
    )
    keymasq_session_unit = (
        fake_root / "root/.config/systemd/user/keymasq-session.service"
    ).read_text(encoding="utf-8")
    assert "KillMode=mixed" in keymasq_session_unit
    assert "WantedBy=default.target" in keymasq_session_unit
    assert "WantedBy=graphical-session.target" in keymasq_session_unit
    assert (fake_root / "opt/keymasq/share/keymasq/appimage-update.gpg.asc").is_file()
    assert "unlock_required = false" in (fake_root / "etc/keymasq/security.toml").read_text(
        encoding="utf-8"
    )
    record_rule = (fake_root / "etc/polkit-1/rules.d/50-keymasq-record.rules").read_text(
        encoding="utf-8"
    )
    assert "/opt/keymasq/bin/keymasq-record" in record_rule
    assert 'action.lookup("user") != "root"' in record_rule
    assert "com.keymasq.record-macro" in record_rule
    assert "polkit.Result.AUTH_SELF" in record_rule
    assert "AUTH_SELF_KEEP" not in record_rule
    assert not (fake_root / "usr/share/polkit-1/actions/com.keymasq.record-macro.policy").exists()
    atomic_keep_list = (fake_root / "etc/atomic-update.conf.d/keymasq.conf").read_text(
        encoding="utf-8"
    )
    assert "/opt/keymasq/**" not in atomic_keep_list
    assert "/etc/atomic-update.conf.d/keymasq.conf" in atomic_keep_list
    assert "/etc/polkit-1/rules.d/50-keymasq-record.rules" in atomic_keep_list
    assert "/etc/profile.d/keymasq.sh" in atomic_keep_list
    assert "/etc/tmpfiles.d/keymasq.conf" in atomic_keep_list
    assert "Exec=/opt/keymasq/bin/keymasq" in (
        fake_root / "root/.local/share/applications/tools.keymasq.keymasq.desktop"
    ).read_text(encoding="utf-8")
    command_log = Path(env["KEYMASQ_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert "systemd-sysusers" in command_log
    assert "systemd-tmpfiles --create" in command_log
    assert "systemctl is-active --quiet keymasqd.service" in command_log
    assert "systemctl daemon-reload" in command_log
    assert "systemctl enable --now keymasqd.service" in command_log
    assert "systemctl try-restart keymasqd.service" not in command_log


def test_appimage_install_scopes_bundled_python_environment_to_python_helpers(
    tmp_path: Path,
) -> None:
    fake_root = tmp_path / "root"
    assets = _asset_dir(tmp_path)
    source_appimage = tmp_path / "Keymasq.AppImage"
    source_appimage.write_text("appimage\n", encoding="utf-8")
    source_appimage.chmod(0o755)
    appdir = tmp_path / "AppDir"
    (appdir / "shared/bin").mkdir(parents=True)
    (appdir / "lib").mkdir()
    (appdir / "share").mkdir()
    _write_executable(appdir / "shared/bin/python3.12", "#!/bin/sh\nexit 99\n")
    _write_executable(appdir / "lib/ld-linux-x86-64.so.2", "#!/bin/sh\nexit 0\n")
    env_log = tmp_path / "command-env.log"
    env = _env(tmp_path, fake_root, assets, source_appimage)
    env["APPDIR"] = str(appdir)
    env["KEYMASQ_APPIMAGE_SERVICE_MANAGER"] = "generic"
    env["KEYMASQ_COMMAND_ENV_LOG"] = str(env_log)
    env["PYTHONPATH"] = "/host/pythonpath"
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("PYTHONHOME", None)

    subprocess.run(
        ["sh", str(RUNTIME_SCRIPT), "--install", "--user", "root"],
        check=True,
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )

    command_env = env_log.read_text(encoding="utf-8")
    assert "udevadm PYTHONHOME=unset" in command_env
    assert "udevadm LD_LIBRARY_PATH=unset" in command_env
    assert "udevadm PYTHONPATH=/host/pythonpath" in command_env


def test_appimage_installer_restarts_services_that_were_already_active(
    tmp_path: Path,
) -> None:
    fake_root = tmp_path / "root"
    (fake_root / "etc").mkdir(parents=True)
    (fake_root / "etc/os-release").write_text("ID=steamos\n", encoding="utf-8")
    assets = _asset_dir(tmp_path)
    source_appimage = tmp_path / "Keymasq.AppImage"
    source_appimage.write_text("appimage\n", encoding="utf-8")
    source_appimage.chmod(0o755)
    env = _env(tmp_path, fake_root, assets, source_appimage)
    env["KEYMASQ_APPIMAGE_SERVICE_MANAGER"] = "systemd"
    env["KEYMASQ_FAKE_SYSTEMD_ACTIVE_STATUS"] = "0"
    env["KEYMASQ_FAKE_USER_SYSTEMD_ACTIVE_STATUS"] = "0"

    subprocess.run(
        ["sh", str(RUNTIME_SCRIPT), "--install", "--user", "root"],
        check=True,
        env=env,
    )

    command_log = Path(env["KEYMASQ_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert "systemctl is-active --quiet keymasqd.service" in command_log
    assert "systemctl try-restart keymasqd.service" in command_log
    assert "systemctl --user is-active --quiet keymasq-session.service" in command_log
    assert "systemctl --user try-restart keymasq-session.service" in command_log
    assert command_log.index("systemctl daemon-reload") < command_log.index(
        "systemctl enable --now keymasqd.service"
    )
    assert command_log.index("systemctl enable --now keymasqd.service") < command_log.index(
        "systemctl try-restart keymasqd.service"
    )


def test_appimage_install_autodetects_systemd_without_steamos_keep_list(
    tmp_path: Path,
) -> None:
    fake_root = tmp_path / "root"
    assets = _asset_dir(tmp_path)
    source_appimage = tmp_path / "Keymasq.AppImage"
    source_appimage.write_text("appimage\n", encoding="utf-8")
    source_appimage.chmod(0o755)
    env = _env(tmp_path, fake_root, assets, source_appimage)
    env["KEYMASQ_APPIMAGE_SERVICE_MANAGER"] = "systemd"

    subprocess.run(
        ["sh", str(RUNTIME_SCRIPT), "--install", "--user", "root"],
        check=True,
        env=env,
    )

    assert (fake_root / "etc/systemd/system/keymasqd.service").is_file()
    assert (fake_root / "root/.config/systemd/user/keymasq-session.service").is_file()
    record_policy = (
        fake_root / "usr/share/polkit-1/actions/com.keymasq.record-macro.policy"
    ).read_text(encoding="utf-8")
    assert "/opt/keymasq/bin/keymasq-record" in record_policy
    assert "auth_self_keep" in record_policy
    assert (fake_root / "etc/polkit-1/rules.d/50-keymasq-record.rules").is_file()
    assert not (fake_root / "etc/atomic-update.conf.d/keymasq.conf").exists()
    assert not (fake_root / "root/.config/autostart/tools.keymasq.keymasq-session.desktop").exists()
    command_log = Path(env["KEYMASQ_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert "systemd-sysusers" in command_log
    assert "systemd-tmpfiles --create" in command_log
    assert "systemctl enable --now keymasqd.service" in command_log
    assert "systemctl try-restart keymasqd.service" not in command_log


def test_appimage_install_creates_systemd_user_dir_without_root_chown(
    tmp_path: Path,
) -> None:
    fake_root = tmp_path / "root"
    assets = _asset_dir(tmp_path)
    source_appimage = tmp_path / "Keymasq.AppImage"
    source_appimage.write_text("appimage\n", encoding="utf-8")
    source_appimage.chmod(0o755)
    env = _env(tmp_path, fake_root, assets, source_appimage)
    env["KEYMASQ_APPIMAGE_SERVICE_MANAGER"] = "systemd"
    current_user = pwd.getpwuid(os.getuid()).pw_name
    current_home = Path(pwd.getpwuid(os.getuid()).pw_dir)

    subprocess.run(
        ["sh", str(RUNTIME_SCRIPT), "--install"],
        check=True,
        env=env,
    )

    fake_home = fake_root / current_home.relative_to("/")
    command_log = Path(env["KEYMASQ_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert "chown " not in command_log
    user_dir_command = f"runuser -u {current_user} -- mkdir {fake_home / '.config'}"
    assert user_dir_command in command_log
    assert command_log.index(user_dir_command) < command_log.index(
        "systemctl --user enable --now keymasq-session.service"
    )


def test_appimage_install_refuses_symlinked_user_directories(
    tmp_path: Path,
) -> None:
    fake_root = tmp_path / "root"
    assets = _asset_dir(tmp_path)
    source_appimage = tmp_path / "Keymasq.AppImage"
    source_appimage.write_text("appimage\n", encoding="utf-8")
    source_appimage.chmod(0o755)
    env = _env(tmp_path, fake_root, assets, source_appimage)
    env["KEYMASQ_APPIMAGE_SERVICE_MANAGER"] = "systemd"
    current_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    fake_home = fake_root / current_home.relative_to("/")
    applications = fake_home / ".local/share/applications"
    symlink_target = tmp_path / "applications-target"
    symlink_target.mkdir()
    applications.parent.mkdir(parents=True)
    applications.symlink_to(symlink_target)

    result = subprocess.run(
        ["sh", str(RUNTIME_SCRIPT), "--install"],
        check=False,
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode != 0
    assert "refusing to write through symlinked user directory" in result.stderr
    assert applications.is_symlink()
    assert not (symlink_target / "tools.keymasq.keymasq.desktop").exists()


def test_appimage_install_does_not_use_root_chown_for_user_directories(
    tmp_path: Path,
) -> None:
    fake_root = tmp_path / "root"
    assets = _asset_dir(tmp_path)
    source_appimage = tmp_path / "Keymasq.AppImage"
    source_appimage.write_text("appimage\n", encoding="utf-8")
    source_appimage.chmod(0o755)
    env = _env(tmp_path, fake_root, assets, source_appimage)
    env["KEYMASQ_APPIMAGE_SERVICE_MANAGER"] = "systemd"
    env["KEYMASQ_FAKE_CHOWN_STATUS"] = "1"

    subprocess.run(["sh", str(RUNTIME_SCRIPT), "--install"], check=True, env=env)

    command_log = Path(env["KEYMASQ_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert "chown " not in command_log


def test_appimage_install_generic_fallback_writes_manual_service_instructions(
    tmp_path: Path,
) -> None:
    fake_root = tmp_path / "root"
    assets = _asset_dir(tmp_path)
    source_appimage = tmp_path / "Keymasq.AppImage"
    source_appimage.write_text("appimage\n", encoding="utf-8")
    source_appimage.chmod(0o755)
    env = _env(tmp_path, fake_root, assets, source_appimage)
    env["KEYMASQ_APPIMAGE_SERVICE_MANAGER"] = "generic"
    env["KEYMASQ_FAKE_UDEVADM_CONTROL_STATUS"] = "1"
    current_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    current_user = pwd.getpwuid(os.getuid()).pw_name

    result = subprocess.run(
        ["sh", str(RUNTIME_SCRIPT), "--install"],
        check=True,
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert (fake_root / "opt/keymasq/Keymasq.AppImage").is_file()
    assert not (fake_root / "etc/systemd/system/keymasqd.service").exists()
    assert not (fake_root / "etc/sysusers.d/keymasq.conf").exists()
    assert not (fake_root / "etc/tmpfiles.d/keymasq.conf").exists()
    assert (fake_root / "run/keymasq").is_dir()
    assert (fake_root / "var/lib/keymasq").is_dir()
    autostart = (
        fake_root
        / current_home.relative_to("/")
        / ".config/autostart/tools.keymasq.keymasq-session.desktop"
    ).read_text(encoding="utf-8")
    assert "KEYMASQ_SESSION_RESTART_ON_DAEMON_DISCONNECT=1" in autostart
    assert "Exec=env " in autostart
    instructions = (fake_root / "opt/keymasq/share/keymasq/non-systemd-services.txt").read_text(
        encoding="utf-8"
    )
    assert "command: /opt/keymasq/bin/keymasqd" in instructions
    assert "user: keymasq" in instructions
    assert "restart policy: restart on failure" in instructions
    assert "install -d -o keymasq -g keymasq -m 0755 /run/keymasq" in instructions
    assert "install -d -o keymasq -g keymasq -m 0750 /var/lib/keymasq" in instructions
    assert "setfacl -m u:keymasq:rw /dev/uinput" in instructions
    assert current_user in instructions
    assert "systemd was not detected" in result.stderr
    assert "could not reload udev rules" in result.stderr
    command_log = Path(env["KEYMASQ_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert "udevadm control --reload-rules" in command_log
    assert "systemctl" not in command_log
    assert "systemd-sysusers" not in command_log
    assert "systemd-tmpfiles" not in command_log


def test_appimage_install_does_not_follow_existing_user_file_symlinks(
    tmp_path: Path,
) -> None:
    fake_root = tmp_path / "root"
    assets = _asset_dir(tmp_path)
    source_appimage = tmp_path / "Keymasq.AppImage"
    source_appimage.write_text("appimage\n", encoding="utf-8")
    source_appimage.chmod(0o755)
    env = _env(tmp_path, fake_root, assets, source_appimage)
    env["KEYMASQ_APPIMAGE_SERVICE_MANAGER"] = "generic"
    current_home = Path(pwd.getpwuid(os.getuid()).pw_dir)

    fake_home = fake_root / current_home.relative_to("/")
    wrapper = fake_home / ".local/bin/keymasq"
    autostart = fake_home / ".config/autostart/tools.keymasq.keymasq-session.desktop"
    autostart_target = tmp_path / "autostart-target"
    autostart_target.write_text("keep-autostart\n", encoding="utf-8")
    autostart.parent.mkdir(parents=True)
    autostart.symlink_to(autostart_target)

    subprocess.run(
        ["sh", str(RUNTIME_SCRIPT), "--install"],
        check=True,
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert autostart_target.read_text(encoding="utf-8") == "keep-autostart\n"
    assert not autostart.is_symlink()
    assert "# Managed by Keymasq AppImage" in wrapper.read_text(encoding="utf-8")
    assert 'exec "$APPDIR/bin/keymasq" "$@"' in wrapper.read_text(encoding="utf-8")
    assert "KEYMASQ_SESSION_RESTART_ON_DAEMON_DISCONNECT=1" in autostart.read_text(encoding="utf-8")


def test_appimage_install_preserves_non_keymasq_user_wrapper(tmp_path: Path) -> None:
    fake_root = tmp_path / "root"
    assets = _asset_dir(tmp_path)
    source_appimage = tmp_path / "Keymasq.AppImage"
    source_appimage.write_text("appimage\n", encoding="utf-8")
    source_appimage.chmod(0o755)
    env = _env(tmp_path, fake_root, assets, source_appimage)
    env["KEYMASQ_APPIMAGE_SERVICE_MANAGER"] = "generic"
    current_home = Path(pwd.getpwuid(os.getuid()).pw_dir)

    wrapper = fake_root / current_home.relative_to("/") / ".local/bin/waypipe"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\necho user-waypipe\n", encoding="utf-8")
    wrapper.chmod(0o755)

    result = subprocess.run(
        ["sh", str(RUNTIME_SCRIPT), "--install"],
        check=False,
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode != 0
    assert f"refusing to replace non-Keymasq user command: {wrapper}" in result.stderr
    assert wrapper.read_text(encoding="utf-8") == "#!/bin/sh\necho user-waypipe\n"
    assert not (fake_root / "opt/keymasq/Keymasq.AppImage").exists()


def test_appimage_uninstall_removes_integration_but_keeps_config_and_state(
    tmp_path: Path,
) -> None:
    fake_root = tmp_path / "root"
    (fake_root / "etc").mkdir(parents=True)
    (fake_root / "etc/os-release").write_text("ID=steamos\n", encoding="utf-8")
    assets = _asset_dir(tmp_path)
    source_appimage = tmp_path / "Keymasq.AppImage"
    source_appimage.write_text("appimage\n", encoding="utf-8")
    source_appimage.chmod(0o755)
    env = _env(tmp_path, fake_root, assets, source_appimage)
    env["KEYMASQ_APPIMAGE_SERVICE_MANAGER"] = "systemd"

    subprocess.run(
        ["sh", str(RUNTIME_SCRIPT), "--install", "--user", "root"],
        check=True,
        env=env,
    )
    macro_path = fake_root / "var/lib/keymasq/macros/example.kmacro.xz"
    macro_path.parent.mkdir(parents=True)
    macro_path.write_text("macro\n", encoding="utf-8")
    local_wrapper = fake_root / "usr/local/bin/keymasq"
    local_wrapper.parent.mkdir(parents=True)
    local_wrapper.write_text("admin-managed\n", encoding="utf-8")
    user_waypipe = fake_root / "root/.local/bin/waypipe"
    user_waypipe.write_text("#!/bin/sh\necho user-waypipe\n", encoding="utf-8")
    user_waypipe.chmod(0o755)
    hidden_flag = fake_root / "run/keymasq/hidden/event0"
    hidden_hardware_flag = fake_root / "run/keymasq/hidden-hardware/28de:1205"
    hidden_flag.parent.mkdir(parents=True, exist_ok=True)
    hidden_hardware_flag.parent.mkdir(parents=True, exist_ok=True)
    hidden_flag.touch()
    hidden_hardware_flag.touch()
    uinput = fake_root / "dev/uinput"
    event = fake_root / "dev/input/event0"
    joystick = fake_root / "dev/input/js0"
    event.parent.mkdir(parents=True)
    uinput.touch()
    event.touch()
    joystick.touch()

    subprocess.run(
        ["sh", str(RUNTIME_SCRIPT), "--uninstall", "--user", "root"],
        check=True,
        env=env,
    )

    assert not (fake_root / "etc/systemd/system/keymasqd.service").exists()
    assert not (fake_root / "etc/tmpfiles.d/keymasq.conf").exists()
    assert not (fake_root / "etc/profile.d/keymasq.sh").exists()
    assert not (fake_root / "etc/udev/rules.d/91-keymasq-acl.rules").exists()
    assert not (fake_root / "etc/polkit-1/rules.d/50-keymasq-record.rules").exists()
    assert not (fake_root / "usr/share/polkit-1/actions/com.keymasq.record-macro.policy").exists()
    assert not (fake_root / "root/.local/bin/keymasq").exists()
    assert user_waypipe.read_text(encoding="utf-8") == "#!/bin/sh\necho user-waypipe\n"
    assert not (fake_root / "opt/keymasq/Keymasq.AppImage").exists()
    assert not (fake_root / "opt/keymasq/runtime").exists()
    assert local_wrapper.read_text(encoding="utf-8") == "admin-managed\n"
    assert (fake_root / "etc/keymasq/security.toml").exists()
    assert macro_path.read_text(encoding="utf-8") == "macro\n"
    assert not hidden_flag.parent.exists()
    assert not hidden_hardware_flag.parent.exists()
    command_log = Path(env["KEYMASQ_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert "udevadm control --reload-rules" in command_log
    assert "udevadm trigger --action=change --subsystem-match=input" in command_log
    assert "udevadm trigger --action=change --sysname-match=uinput" in command_log
    assert "udevadm settle" in command_log
    assert f"setfacl -x u:keymasq {uinput}" in command_log
    assert f"setfacl -x u:keymasq {event}" in command_log
    assert f"setfacl -x u:keymasq {joystick}" in command_log


def test_appimage_runtime_exports_gtk_introspection_environment(tmp_path: Path) -> None:
    appdir = tmp_path / "AppDir"
    log_path = tmp_path / "runtime-env.log"
    (appdir / "shared/bin").mkdir(parents=True)
    (appdir / "lib/girepository-1.0").mkdir(parents=True)
    (appdir / "lib/gdk-pixbuf-2.0/2.10.0/loaders").mkdir(parents=True)
    (appdir / "lib/gdk-pixbuf-2.0/2.10.0/loaders.cache").write_text(
        "loaders\n",
        encoding="utf-8",
    )
    (appdir / "lib/gio/modules").mkdir(parents=True)
    (appdir / "share").mkdir()
    (appdir / "share/X11/xkb").mkdir(parents=True)
    _write_executable(appdir / "shared/bin/python3.12", "#!/bin/sh\nexit 99\n")
    _write_executable(
        appdir / "lib/ld-linux-x86-64.so.2",
        """#!/bin/sh
{
  printf 'GI_TYPELIB_PATH=%s\\n' "$GI_TYPELIB_PATH"
  printf 'GDK_PIXBUF_MODULE_FILE=%s\\n' "$GDK_PIXBUF_MODULE_FILE"
  printf 'GDK_PIXBUF_MODULEDIR=%s\\n' "$GDK_PIXBUF_MODULEDIR"
  printf 'GIO_MODULE_DIR=%s\\n' "$GIO_MODULE_DIR"
  printf 'XKB_CONFIG_ROOT=%s\\n' "$XKB_CONFIG_ROOT"
  printf 'XDG_DATA_DIRS=%s\\n' "$XDG_DATA_DIRS"
  printf 'PYTHONHOME=%s\\n' "$PYTHONHOME"
  printf 'PYTHONNOUSERSITE=%s\\n' "$PYTHONNOUSERSITE"
  printf 'PYTHONPATH=%s\\n' "${PYTHONPATH-unset}"
  printf 'LD_LIBRARY_PATH=%s\\n' "$LD_LIBRARY_PATH"
  printf 'GSK_RENDERER=%s\\n' "${GSK_RENDERER:-}"
  printf 'GDK_DISABLE=%s\\n' "${GDK_DISABLE:-}"
  printf 'GDK_GL=%s\\n' "${GDK_GL:-}"
  printf 'QT_QUICK_BACKEND=%s\\n' "${QT_QUICK_BACKEND:-}"
  printf 'argv=%s\\n' "$*"
} > "$KEYMASQ_RUNTIME_ENV_LOG"
exit 0
""",
    )
    env = os.environ.copy()
    env.update(
        {
            "APPDIR": str(appdir),
            "KEYMASQ_RUNTIME_ENV_LOG": str(log_path),
            "PYTHONPATH": "/host/pythonpath",
            "XDG_DATA_DIRS": "/usr/local/share:/usr/share",
        }
    )
    for key in ("GSK_RENDERER", "GDK_DISABLE", "GDK_GL", "QT_QUICK_BACKEND"):
        env.pop(key, None)

    subprocess.run(["sh", str(RUNTIME_SCRIPT), "keymasq"], check=True, env=env)

    runtime_env = log_path.read_text(encoding="utf-8")
    assert f"GI_TYPELIB_PATH={appdir}/lib/girepository-1.0" in runtime_env
    assert f"GDK_PIXBUF_MODULE_FILE={appdir}/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache" in runtime_env
    assert f"GDK_PIXBUF_MODULEDIR={appdir}/lib/gdk-pixbuf-2.0/2.10.0/loaders" in runtime_env
    assert f"GIO_MODULE_DIR={appdir}/lib/gio/modules" in runtime_env
    assert f"XKB_CONFIG_ROOT={appdir}/share/X11/xkb" in runtime_env
    assert f"XDG_DATA_DIRS={appdir}/share:/usr/local/share:/usr/share" in runtime_env
    assert f"PYTHONHOME={appdir}" in runtime_env
    assert "PYTHONNOUSERSITE=true" in runtime_env
    assert "PYTHONPATH=unset" in runtime_env
    assert f"LD_LIBRARY_PATH={appdir}/lib" in runtime_env
    assert "GSK_RENDERER=\n" in runtime_env
    assert "GDK_DISABLE=\n" in runtime_env
    assert "GDK_GL=\n" in runtime_env
    assert "QT_QUICK_BACKEND=\n" in runtime_env
    assert "argv=--library-path" in runtime_env


def test_appimage_runtime_can_force_software_rendering(tmp_path: Path) -> None:
    appdir = tmp_path / "AppDir"
    log_path = tmp_path / "runtime-env.log"
    (appdir / "shared/bin").mkdir(parents=True)
    (appdir / "lib").mkdir()
    (appdir / "share").mkdir()
    _write_executable(appdir / "shared/bin/python3.12", "#!/bin/sh\nexit 99\n")
    _write_executable(
        appdir / "lib/ld-linux-x86-64.so.2",
        """#!/bin/sh
{
  printf 'GSK_RENDERER=%s\\n' "${GSK_RENDERER:-}"
  printf 'GDK_DISABLE=%s\\n' "${GDK_DISABLE:-}"
  printf 'GDK_GL=%s\\n' "${GDK_GL:-}"
  printf 'QT_QUICK_BACKEND=%s\\n' "${QT_QUICK_BACKEND:-}"
} > "$KEYMASQ_RUNTIME_ENV_LOG"
exit 0
""",
    )
    env = os.environ.copy()
    env.update(
        {
            "APPDIR": str(appdir),
            "KEYMASQ_APPIMAGE_RENDERING": "software",
            "KEYMASQ_RUNTIME_ENV_LOG": str(log_path),
        }
    )

    subprocess.run(["sh", str(RUNTIME_SCRIPT), "keymasq"], check=True, env=env)

    runtime_env = log_path.read_text(encoding="utf-8")
    assert "GSK_RENDERER=cairo\n" in runtime_env
    assert "GDK_DISABLE=gl,vulkan\n" in runtime_env
    assert "GDK_GL=disable\n" in runtime_env
    assert "QT_QUICK_BACKEND=software\n" in runtime_env


def test_appimage_runtime_uses_private_gtk_when_brotway_is_enabled(
    tmp_path: Path,
) -> None:
    appdir = tmp_path / "AppDir"
    log_path = tmp_path / "runtime-env.log"
    (appdir / "shared/bin").mkdir(parents=True)
    (appdir / "lib/gtk4-brotway").mkdir(parents=True)
    (appdir / "share").mkdir()
    _write_executable(appdir / "shared/bin/python3.12", "#!/bin/sh\nexit 99\n")
    _write_executable(
        appdir / "lib/ld-linux-x86-64.so.2",
        """#!/bin/sh
printf 'LD_LIBRARY_PATH=%s\nargv=%s\n' "$LD_LIBRARY_PATH" "$*" > "$KEYMASQ_RUNTIME_ENV_LOG"
exit 0
""",
    )
    env = os.environ.copy()
    env.update(
        {
            "APPDIR": str(appdir),
            "KEYMASQ_APPIMAGE_BROTWAY": "1",
            "KEYMASQ_RUNTIME_ENV_LOG": str(log_path),
        }
    )
    env.pop("LD_LIBRARY_PATH", None)

    subprocess.run(["sh", str(RUNTIME_SCRIPT), "keymasq"], check=True, env=env)

    runtime_env = log_path.read_text(encoding="utf-8")
    expected = f"{appdir}/lib/gtk4-brotway:{appdir}/lib"
    assert f"LD_LIBRARY_PATH={expected}\n" in runtime_env
    assert f"argv=--library-path {expected} " in runtime_env


def test_appimage_self_update_verifies_signed_manifest(tmp_path: Path) -> None:
    fake_root = tmp_path / "root"
    assets = _asset_dir(tmp_path)
    source_appimage = tmp_path / "source.AppImage"
    source_appimage.write_text("source\n", encoding="utf-8")
    env = _env(tmp_path, fake_root, assets, source_appimage)
    new_assets = Path(env["KEYMASQ_APPIMAGE_EXTRACTED_SOURCE_DIR"]) / "share/keymasq/appimage"
    (new_assets / "appimage-update.gpg.asc").write_text(
        "new-update-public-key\n",
        encoding="utf-8",
    )
    target = fake_root / "opt/keymasq/Keymasq.AppImage"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o755)
    stale_paths = (
        fake_root / "etc/systemd/system/keymasqd.service",
        fake_root / "root/.config/systemd/user/keymasq-session.service",
        fake_root / "etc/atomic-update.conf.d/keymasq.conf",
        fake_root / "opt/keymasq/bin/keymasq",
        fake_root / "opt/keymasq/share/keymasq/appimage-update.gpg.asc",
    )
    for path in stale_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stale\n", encoding="utf-8")

    public_key = tmp_path / "update-public.asc"
    public_key.write_text("test-public-key\n", encoding="utf-8")

    update_dir = tmp_path / "updates"
    update_dir.mkdir()
    new_appimage = update_dir / "Keymasq-9.9.9-x86_64.AppImage"
    new_appimage.write_text("new\n", encoding="utf-8")
    sha256 = _write_update_manifest(update_dir, new_appimage, "9.9.9")
    (update_dir / "latest-x86_64.json.sig").write_text("trusted-signature\n", encoding="utf-8")
    _install_fake_gpg(env)
    env["KEYMASQ_APPIMAGE_UPDATE_BASE_URL"] = update_dir.as_uri()
    env["KEYMASQ_APPIMAGE_UPDATE_PUBLIC_KEY"] = str(public_key)
    env["KEYMASQ_APPIMAGE_CURRENT_VERSION"] = "1.0.0"
    env["KEYMASQ_FAKE_SYSTEMD_ENABLED_STATUS"] = "0"
    env["KEYMASQ_FAKE_USER_SYSTEMD_ENABLED_STATUS"] = "0"

    runtime_link = tmp_path / "keymasq"
    runtime_link.symlink_to(RUNTIME_SCRIPT)
    subprocess.run([str(runtime_link), "--self-update", "--user", "root"], check=True, env=env)

    assert target.read_text(encoding="utf-8") == "new\n"
    assert (fake_root / "opt/keymasq/runtime/current").readlink() == Path(sha256)
    assert (fake_root / f"opt/keymasq/runtime/{sha256}/bin/keymasq").is_file()
    assert (fake_root / "opt/keymasq/version").read_text(encoding="utf-8") == "9.9.9\n"
    assert "ExecStart=/opt/keymasq/bin/keymasqd" in (
        fake_root / "etc/systemd/system/keymasqd.service"
    ).read_text(encoding="utf-8")
    assert "WantedBy=graphical-session.target" in (
        fake_root / "root/.config/systemd/user/keymasq-session.service"
    ).read_text(encoding="utf-8")
    assert (fake_root / "opt/keymasq/share/keymasq/appimage-update.gpg.asc").read_text(
        encoding="utf-8"
    ) == "new-update-public-key\n"
    assert 'exec "$APPDIR/bin/keymasq" "$@"' in (fake_root / "opt/keymasq/bin/keymasq").read_text(
        encoding="utf-8"
    )
    assert "/etc/systemd/system/keymasqd.service" in (
        fake_root / "etc/atomic-update.conf.d/keymasq.conf"
    ).read_text(encoding="utf-8")
    command_log = Path(env["KEYMASQ_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert "systemctl reenable keymasqd.service" in command_log
    assert "systemctl --user reenable keymasq-session.service" in command_log


def test_appimage_self_update_rejects_signed_cross_architecture_manifest(
    tmp_path: Path,
) -> None:
    fake_root = tmp_path / "root"
    assets = _asset_dir(tmp_path)
    source_appimage = tmp_path / "source.AppImage"
    source_appimage.write_text("source\n", encoding="utf-8")
    env = _env(tmp_path, fake_root, assets, source_appimage)
    target = fake_root / "opt/keymasq/Keymasq.AppImage"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o755)

    public_key = tmp_path / "update-public.asc"
    public_key.write_text("test-public-key\n", encoding="utf-8")

    update_dir = tmp_path / "updates"
    update_dir.mkdir()
    wrong_arch_appimage = update_dir / "Keymasq-9.9.9-aarch64.AppImage"
    wrong_arch_appimage.write_text("wrong architecture\n", encoding="utf-8")
    _write_update_manifest(
        update_dir,
        wrong_arch_appimage,
        "9.9.9",
        architecture="aarch64",
    )
    wrong_arch_appimage.unlink()
    (update_dir / "latest-x86_64.json.sig").write_text(
        "trusted-signature\n",
        encoding="utf-8",
    )
    _install_fake_gpg(env)
    env["KEYMASQ_APPIMAGE_UPDATE_BASE_URL"] = update_dir.as_uri()
    env["KEYMASQ_APPIMAGE_UPDATE_PUBLIC_KEY"] = str(public_key)
    env["KEYMASQ_APPIMAGE_ARCH"] = "x86_64"
    env["KEYMASQ_APPIMAGE_CURRENT_VERSION"] = "1.0.0"

    runtime_link = tmp_path / "keymasq"
    runtime_link.symlink_to(RUNTIME_SCRIPT)
    result = subprocess.run(
        [str(runtime_link), "--self-update", "--user", "root"],
        check=False,
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode != 0
    assert (
        "update manifest architecture aarch64 does not match system architecture x86_64"
        in result.stderr
    )
    assert "gpg --homedir" in Path(env["KEYMASQ_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "old\n"
    assert not (fake_root / "opt/keymasq/runtime/current").exists()


def test_appimage_self_update_rejects_signed_downgrade(tmp_path: Path) -> None:
    fake_root = tmp_path / "root"
    assets = _asset_dir(tmp_path)
    source_appimage = tmp_path / "source.AppImage"
    source_appimage.write_text("source\n", encoding="utf-8")
    env = _env(tmp_path, fake_root, assets, source_appimage)
    target = fake_root / "opt/keymasq/Keymasq.AppImage"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o755)

    public_key = tmp_path / "update-public.asc"
    public_key.write_text("test-public-key\n", encoding="utf-8")

    update_dir = tmp_path / "updates"
    update_dir.mkdir()
    new_appimage = update_dir / "Keymasq-9.8.0-x86_64.AppImage"
    new_appimage.write_text("downgrade\n", encoding="utf-8")
    _write_update_manifest(update_dir, new_appimage, "9.8.0")
    (update_dir / "latest-x86_64.json.sig").write_text("trusted-signature\n", encoding="utf-8")
    _install_fake_gpg(env)
    env["KEYMASQ_APPIMAGE_UPDATE_BASE_URL"] = update_dir.as_uri()
    env["KEYMASQ_APPIMAGE_UPDATE_PUBLIC_KEY"] = str(public_key)
    env["KEYMASQ_APPIMAGE_CURRENT_VERSION"] = "9.9.9"

    runtime_link = tmp_path / "keymasq"
    runtime_link.symlink_to(RUNTIME_SCRIPT)
    result = subprocess.run(
        [str(runtime_link), "--self-update", "--user", "root"],
        check=False,
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode != 0
    assert "refusing to downgrade from 9.9.9 to 9.8.0" in result.stderr
    assert target.read_text(encoding="utf-8") == "old\n"
    assert not (fake_root / "opt/keymasq/runtime/current").exists()


def test_appimage_self_update_does_not_activate_when_integration_refresh_fails(
    tmp_path: Path,
) -> None:
    fake_root = tmp_path / "root"
    assets = _asset_dir(tmp_path)
    source_appimage = tmp_path / "source.AppImage"
    source_appimage.write_text("source\n", encoding="utf-8")
    env = _env(tmp_path, fake_root, assets, source_appimage)

    install_root = fake_root / "opt/keymasq"
    runtime_root = install_root / "runtime"
    old_runtime = runtime_root / "old-runtime"
    old_runtime.mkdir(parents=True)
    (runtime_root / "current").symlink_to("old-runtime")
    target = install_root / "Keymasq.AppImage"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o755)
    version_file = install_root / "version"
    version_file.write_text("1.0.0\n", encoding="utf-8")

    blocked_user_dir = fake_root / "root/.local"
    blocked_user_dir.parent.mkdir(parents=True)
    blocked_user_dir.symlink_to(tmp_path)

    update_dir = tmp_path / "updates"
    update_dir.mkdir()
    new_appimage = update_dir / "Keymasq-2.0.0-x86_64.AppImage"
    new_appimage.write_text("new\n", encoding="utf-8")
    _write_update_manifest(update_dir, new_appimage, "2.0.0")
    env["KEYMASQ_APPIMAGE_UPDATE_BASE_URL"] = update_dir.as_uri()

    runtime_link = tmp_path / "keymasq"
    runtime_link.symlink_to(RUNTIME_SCRIPT)
    result = subprocess.run(
        [
            str(runtime_link),
            "--self-update",
            "--allow-unsigned",
            "--user",
            "root",
        ],
        check=False,
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode != 0
    assert "refusing to write through symlinked user directory" in result.stderr
    assert target.read_text(encoding="utf-8") == "old\n"
    assert (runtime_root / "current").readlink() == Path("old-runtime")
    assert version_file.read_text(encoding="utf-8") == "1.0.0\n"
    assert not list(install_root.glob("Keymasq.AppImage.new.*"))


def test_appimage_self_update_reports_service_restart_failure(tmp_path: Path) -> None:
    fake_root = tmp_path / "root"
    assets = _asset_dir(tmp_path)
    source_appimage = tmp_path / "source.AppImage"
    source_appimage.write_text("source\n", encoding="utf-8")
    env = _env(tmp_path, fake_root, assets, source_appimage)

    target = fake_root / "opt/keymasq/Keymasq.AppImage"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o755)
    (fake_root / "etc/systemd/system").mkdir(parents=True)
    (fake_root / "etc/systemd/system/keymasqd.service").write_text("stale\n", encoding="utf-8")

    update_dir = tmp_path / "updates"
    update_dir.mkdir()
    new_appimage = update_dir / "Keymasq-2.0.0-x86_64.AppImage"
    new_appimage.write_text("new\n", encoding="utf-8")
    _write_update_manifest(update_dir, new_appimage, "2.0.0")
    env["KEYMASQ_APPIMAGE_UPDATE_BASE_URL"] = update_dir.as_uri()
    env["KEYMASQ_APPIMAGE_CURRENT_VERSION"] = "1.0.0"
    env["KEYMASQ_FAKE_SYSTEMD_RESTART_STATUS"] = "1"

    runtime_link = tmp_path / "keymasq"
    runtime_link.symlink_to(RUNTIME_SCRIPT)
    result = subprocess.run(
        [
            str(runtime_link),
            "--self-update",
            "--allow-unsigned",
            "--user",
            "root",
        ],
        check=False,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert target.read_text(encoding="utf-8") == "new\n"
    assert "could not restart keymasqd.service after update" in result.stderr
    assert "files were updated to 2.0.0" in result.stderr
    assert "Keymasq updated to 2.0.0" not in result.stdout


def test_appimage_self_update_allows_explicit_downgrade(tmp_path: Path) -> None:
    fake_root = tmp_path / "root"
    assets = _asset_dir(tmp_path)
    source_appimage = tmp_path / "source.AppImage"
    source_appimage.write_text("source\n", encoding="utf-8")
    env = _env(tmp_path, fake_root, assets, source_appimage)
    target = fake_root / "opt/keymasq/Keymasq.AppImage"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o755)

    update_dir = tmp_path / "updates"
    update_dir.mkdir()
    new_appimage = update_dir / "Keymasq-9.8.0-x86_64.AppImage"
    new_appimage.write_text("downgrade\n", encoding="utf-8")
    sha256 = _write_update_manifest(update_dir, new_appimage, "9.8.0")
    env["KEYMASQ_APPIMAGE_UPDATE_BASE_URL"] = update_dir.as_uri()
    env["KEYMASQ_APPIMAGE_CURRENT_VERSION"] = "9.9.9"

    runtime_link = tmp_path / "keymasq"
    runtime_link.symlink_to(RUNTIME_SCRIPT)
    subprocess.run(
        [
            str(runtime_link),
            "--self-update",
            "--allow-unsigned",
            "--allow-downgrade",
            "--user",
            "root",
        ],
        check=True,
        env=env,
    )

    assert target.read_text(encoding="utf-8") == "downgrade\n"
    assert (fake_root / "opt/keymasq/runtime/current").readlink() == Path(sha256)
    assert (fake_root / "opt/keymasq/version").read_text(encoding="utf-8") == "9.8.0\n"


def test_appimage_self_update_can_skip_signature_for_unsigned_test_manifests(
    tmp_path: Path,
) -> None:
    fake_root = tmp_path / "root"
    assets = _asset_dir(tmp_path)
    source_appimage = tmp_path / "source.AppImage"
    source_appimage.write_text("source\n", encoding="utf-8")
    env = _env(tmp_path, fake_root, assets, source_appimage)
    target = fake_root / "opt/keymasq/Keymasq.AppImage"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o755)

    update_dir = tmp_path / "updates"
    update_dir.mkdir()
    new_appimage = update_dir / "Keymasq-9.9.9-x86_64.AppImage"
    new_appimage.write_text("new\n", encoding="utf-8")
    sha256 = _write_update_manifest(update_dir, new_appimage, "9.9.9")
    fake_bin = Path(env["PATH"].split(":", 1)[0])
    _write_executable(
        fake_bin / "gpg",
        """#!/bin/sh
printf '%s %s\\n' "${0##*/}" "$*" >> "$KEYMASQ_COMMAND_LOG"
exit 97
""",
    )
    env["KEYMASQ_APPIMAGE_UPDATE_BASE_URL"] = update_dir.as_uri()
    env["KEYMASQ_APPIMAGE_CURRENT_VERSION"] = "1.0.0"

    runtime_link = tmp_path / "keymasq"
    runtime_link.symlink_to(RUNTIME_SCRIPT)
    subprocess.run(
        [str(runtime_link), "--self-update", "--allow-unsigned", "--user", "root"],
        check=True,
        env=env,
    )

    assert target.read_text(encoding="utf-8") == "new\n"
    assert (fake_root / "opt/keymasq/runtime/current").readlink() == Path(sha256)
    assert (fake_root / f"opt/keymasq/runtime/{sha256}/bin/keymasq").is_file()
    command_log = Path(env["KEYMASQ_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert "gpg " not in command_log
