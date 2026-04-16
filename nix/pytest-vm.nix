{ pkgs, system, keymasqPackage, keymasqModule, source }:

let
  lib = pkgs.lib;
  vmUser = "keymasqvm";
  vmUid = 1000;
  runtimeDir = "/run/user/${toString vmUid}";
  worktreeDir = "/tmp/keymasq-src";
  sessionEnvFile = "/home/${vmUser}/.pytest-vm-session-env";
  testPython = pkgs.python3.withPackages (
    ps: with ps; [
      dbus-next
      evdev
      pygobject3
      pytest
      pytest-asyncio
      pytest-cov
      tomli-w
      xlib
    ]
  );
  pytestRunner = pkgs.stdenvNoCC.mkDerivation {
    pname = "keymasq-pytest-vm-runner";
    version = "1";
    dontUnpack = true;

    nativeBuildInputs = [
      pkgs.gobject-introspection
      pkgs.wrapGAppsHook4
    ];

    buildInputs = [
      testPython
      pkgs.adwaita-icon-theme
      pkgs.cairo
      pkgs.gdk-pixbuf
      pkgs.glib
      pkgs.graphene
      pkgs.gtk4
      pkgs.harfbuzz
      pkgs.hicolor-icon-theme
      pkgs.libadwaita
      pkgs.pango.out
    ];

    installPhase = ''
      mkdir -p "$out/bin"
      cat > "$out/bin/keymasq-pytest-vm" <<'EOF'
#!${pkgs.bash}/bin/bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
if [ -n "${"$"}{PYTHONPATH-}" ]; then
  export PYTHONPATH="${"$"}{PYTHONPATH}:$PWD"
else
  export PYTHONPATH="$PWD"
fi
exec ${testPython}/bin/python -m pytest "$@"
EOF
      chmod +x "$out/bin/keymasq-pytest-vm"
    '';
  };
  userCommand =
    cmd:
    "runuser -u ${vmUser} -- sh -lc 'export HOME=/home/${vmUser}; "
    + "export XDG_RUNTIME_DIR=${runtimeDir}; "
    + "export DBUS_SESSION_BUS_ADDRESS=unix:path=${runtimeDir}/bus; "
    + "if [ -f ${sessionEnvFile} ]; then . ${sessionEnvFile}; fi; "
    + "export DISPLAY=${"$"}{DISPLAY:-:0}; "
    + "${cmd}'";
in
{
  checks = {
    pytest-vm = pkgs.testers.runNixOSTest {
      name = "pytest-vm";

      nodes.machine =
        { ... }:
        {
          imports = [ keymasqModule ];

          documentation.nixos.enable = false;

          virtualisation = {
            graphics = true;
            memorySize = 4096;
            cores = 2;
          };

          networking.hostName = "pytest-vm";
          time.timeZone = "UTC";
          i18n.defaultLocale = "en_US.UTF-8";

          boot.kernelModules = [ "uinput" ];

          services.keymasq = {
            enable = true;
            securityConfig = {
              daemon_allowed_uids = [ vmUid ];
              session_allowed_uids = [ vmUid ];
              recording_guard = {
                unlock_required = false;
                macro_edit_requires_unlock = false;
              };
            };
          };

          users.users.${vmUser} = {
            isNormalUser = true;
            uid = vmUid;
            description = "Pytest VM user";
            createHome = true;
            home = "/home/${vmUser}";
            extraGroups = [ "wheel" "video" "input" ];
          };

          services.xserver.enable = true;
          services.xserver.displayManager.lightdm.enable = true;
          services.xserver.desktopManager.xfce.enable = true;
          services.displayManager.defaultSession = "xfce";
          services.displayManager.autoLogin = {
            enable = true;
            user = vmUser;
          };

          hardware.graphics.enable = true;
          services.dbus.enable = true;
          security.polkit.enable = true;
          services.libinput.enable = true;
          programs.dconf.enable = true;

          environment.systemPackages = [
            keymasqPackage
            pytestRunner
            testPython
            pkgs.gobject-introspection
            pkgs.gtk4
            pkgs.libadwaita
            pkgs.adwaita-icon-theme
            pkgs.hicolor-icon-theme
          ];
        };

      testScript = ''
        import os
        import re
        import shlex
        import time

        pytest_output_path = "/tmp/pytest-vm-output.log"
        pytest_status_path = "/tmp/pytest-vm-exit.txt"
        pytest_mark_expr = os.environ.get("KEYMASQ_PYTEST_MARK_EXPR", "").strip()
        pytest_mark_args = (
            " -m " + shlex.quote(pytest_mark_expr)
            if pytest_mark_expr
            else ""
        )

        def as_user(cmd: str) -> str:
            return "${userCommand "{cmd}"}".replace("{cmd}", cmd)

        def wait_for_command(label: str, command: str, timeout: int = 120) -> None:
            deadline = time.time() + timeout
            while time.time() < deadline:
                rc = machine.execute(command)[0]
                if rc == 0:
                    return
                time.sleep(1)
            raise Exception(f"Timed out waiting for {label}: {command}")

        def wait_for_user_command(label: str, command: str, timeout: int = 120) -> None:
            wait_for_command(label, as_user(command), timeout=timeout)

        def must_run(command: str, timeout: int | None = None) -> str:
            if timeout is None:
                rc, output = machine.execute(command)
            else:
                rc, output = machine.execute(command, timeout=timeout)
            if rc != 0:
                raise Exception(f"Command failed with exit code {rc}: {command}\\n{output}")
            return output

        serial_stdout_off()
        machine.wait_for_unit("display-manager.service")
        wait_for_user_command("xfce session", "pgrep -u ${toString vmUid} xfce4-session")
        wait_for_user_command("runtime dir", "test -d ${runtimeDir}")
        wait_for_user_command("session bus", "test -S ${runtimeDir}/bus")
        session_pid = must_run("pgrep -u ${toString vmUid} xfce4-session | head -n 1").strip()
        session_env_raw = must_run(f"tr '\\0' '\\n' </proc/{session_pid}/environ")
        session_env = {}
        for line in session_env_raw.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            session_env[key] = value

        session_exports = {
            "DISPLAY": session_env.get("DISPLAY", ":0"),
            "XAUTHORITY": session_env.get("XAUTHORITY", "/home/${vmUser}/.Xauthority"),
            "XDG_SESSION_TYPE": session_env.get("XDG_SESSION_TYPE", ""),
            "XDG_CURRENT_DESKTOP": session_env.get("XDG_CURRENT_DESKTOP", ""),
            "WAYLAND_DISPLAY": session_env.get("WAYLAND_DISPLAY", ""),
        }
        env_file_body = "\n".join(
            f"export {key}={shlex.quote(value)}"
            for key, value in session_exports.items()
            if value
        )
        must_run(
            "cat > ${sessionEnvFile} <<'EOF'\n"
            + env_file_body
            + "\nEOF"
        )
        must_run("chown ${vmUser}:users ${sessionEnvFile}")
        must_run("chmod 600 ${sessionEnvFile}")
        must_run("modprobe uinput")
        wait_for_command("uinput device", "test -c /dev/uinput")
        must_run("chgrp input /dev/uinput")
        must_run("chmod g+rw /dev/uinput")
        wait_for_user_command("uinput writable", "test -w /dev/uinput")

        must_run("rm -rf ${worktreeDir}")
        must_run("cp -r ${source} ${worktreeDir}")
        must_run("chmod -R u+w ${worktreeDir}")
        must_run("chown -R ${vmUser}:users ${worktreeDir}")

        must_run(
            as_user(
                "cd ${worktreeDir} && "
                "rm -f /tmp/pytest-vm-output.log /tmp/pytest-vm-exit.txt && "
                "set +e; "
                "${pytestRunner}/bin/keymasq-pytest-vm tests/ -q -ra"
                + pytest_mark_args
                + " > /tmp/pytest-vm-output.log 2>&1; "
                "status=$?; "
                "echo \"$status\" > /tmp/pytest-vm-exit.txt; "
                "exit 0"
            ),
            timeout=3600,
        )

        pytest_rc = int(must_run(as_user("cat /tmp/pytest-vm-exit.txt")).strip())
        pytest_output = must_run(as_user("cat /tmp/pytest-vm-output.log"))
        pytest_lines = [line.rstrip() for line in pytest_output.splitlines() if line.strip()]

        summary_lines = []
        warnings_summary_index = next(
            (
                index
                for index, line in enumerate(pytest_lines)
                if "warnings summary" in line.lower()
            ),
            None,
        )
        if warnings_summary_index is not None:
            warnings_end_index = next(
                (
                    index
                    for index, line in enumerate(pytest_lines[warnings_summary_index:], start=warnings_summary_index)
                    if line.startswith("-- Docs:")
                ),
                len(pytest_lines) - 1,
            )
            warning_counts = {}
            warning_order = []
            for line in pytest_lines[warnings_summary_index + 1 : warnings_end_index]:
                stripped = line.strip()
                warning_match = re.search(
                    r"(?P<path>.*?\.py:\d+): (?P<category>\w+Warning): (?P<message>.+)",
                    stripped,
                )
                if warning_match is None:
                    continue

                path = warning_match.group("path")
                if "/site-packages/" in path:
                    short_path = path.split("/site-packages/", 1)[1]
                else:
                    short_path = path

                warning_key = (
                    f"{short_path}: {warning_match.group('category')}: "
                    f"{warning_match.group('message')}"
                )
                if warning_key not in warning_counts:
                    warning_counts[warning_key] = 0
                    warning_order.append(warning_key)
                warning_counts[warning_key] += 1

            for warning_key in warning_order:
                count = warning_counts[warning_key]
                if count == 1:
                    summary_lines.append(warning_key)
                else:
                    summary_lines.append(f"{count}x {warning_key}")

        short_summary_index = next(
            (
                index
                for index, line in enumerate(pytest_lines)
                if "short test summary info" in line.lower()
            ),
            None,
        )
        if short_summary_index is not None:
            summary_lines.extend(pytest_lines[short_summary_index:])
        else:
            final_summary = next(
                (
                    line
                    for line in reversed(pytest_lines)
                    if any(token in line for token in (" passed", " failed", " error", " skipped"))
                ),
                pytest_lines[-1] if pytest_lines else "pytest produced no output",
            )
            summary_lines.append(final_summary)

        print("pytest results:\n" + "\n".join(summary_lines))
        serial_stdout_on()

        if pytest_rc != 0:
            raise Exception("pytest failed inside pytest-vm")
      '';
    };
  };
}
