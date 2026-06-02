{ pkgs, system, keymasqPackage, keymasqModule }:

let
  vmUser = "keymasqvm";
  vmUid = 1000;
  runtimeDir = "/run/user/${toString vmUid}";
  testSource = ./daemon-session-integration-test;
  testPython = pkgs.python3.withPackages (ps: [ ps.evdev ]);

  integrationRunner = pkgs.writeShellApplication {
    name = "keymasq-daemon-session-integration-test";
    runtimeInputs = [
      pkgs.systemd
      testPython
    ];
    text = ''
      export PYTHONPATH="${testSource}"
      export KEYMASQ_INTEGRATION_SYSTEMCTL="${pkgs.systemd}/bin/systemctl"
      export KEYMASQ_INTEGRATION_SUDO="/run/wrappers/bin/sudo"
      export KEYMASQ_INTEGRATION_RECORD_HELPER="${keymasqPackage}/bin/keymasq-record"
      exec ${testPython}/bin/python ${testSource}/runner.py
    '';
  };

  userCommand =
    cmd:
    "runuser -u ${vmUser} -- sh -lc 'export HOME=/home/${vmUser}; "
    + "export XDG_RUNTIME_DIR=${runtimeDir}; "
    + "export DBUS_SESSION_BUS_ADDRESS=unix:path=${runtimeDir}/bus; "
    + "${cmd}'";
  mkDaemonSessionIntegrationTest =
    {
      name,
      unlockRequired,
      scenarioFilter ? "",
    }:
    let
      scenarioEnv =
        if scenarioFilter == "" then "" else "KEYMASQ_INTEGRATION_SCENARIOS=${scenarioFilter} ";
    in
    pkgs.testers.runNixOSTest {
      inherit name;

      nodes.machine =
        { ... }:
        {
          imports = [ keymasqModule ];

          documentation.nixos.enable = false;

          virtualisation = {
            graphics = false;
            memorySize = 2048;
            cores = 2;
          };

          networking.hostName = "daemon-session-integration-test";
          time.timeZone = "UTC";
          i18n.defaultLocale = "en_US.UTF-8";

          boot.kernelModules = [ "uinput" ];

          services.keymasq = {
            enable = true;
            securityConfig = {
              daemon_allowed_uids = [ vmUid ];
              session_allowed_uids = [ vmUid ];
              recording_guard = {
                unlock_required = unlockRequired;
                macro_edit_requires_unlock = false;
              };
            };
          };

          systemd.services.keymasqd.serviceConfig.Environment = [ "KEYMASQ_TEST_UINPUT=1" ];

          users.users.${vmUser} = {
            isNormalUser = true;
            uid = vmUid;
            description = "Daemon/session integration test VM user";
            createHome = true;
            home = "/home/${vmUser}";
            extraGroups = [ "input" ];
          };

          services.dbus.enable = true;
          security = {
            polkit.enable = true;
            sudo = {
              enable = true;
              extraRules = [
                {
                  users = [ vmUser ];
                  commands = [
                    {
                      command = "${pkgs.systemd}/bin/systemctl restart keymasqd.service";
                      options = [ "NOPASSWD" ];
                    }
                    {
                      command = "${keymasqPackage}/bin/keymasq-record";
                      options = [ "NOPASSWD" ];
                    }
                  ];
                }
              ];
            };
          };

          environment.systemPackages = [
            keymasqPackage
            integrationRunner
            testPython
          ];
        };

      testScript = ''
        import time

        output_path = "/tmp/daemon-session-integration-test-output.log"
        status_path = "/tmp/daemon-session-integration-test-status.txt"

        def as_user(cmd: str) -> str:
            return "${userCommand "{cmd}"}".replace("{cmd}", cmd)

        def log_command_output(label: str, cmd: str) -> None:
            status, output = machine.execute(cmd)
            machine.log(f"{label} (exit={status})\n{output}")

        def dump_debug(label: str) -> None:
            machine.log(f"==== {label} ====")
            log_command_output("keymasqd status", "systemctl status keymasqd.service --no-pager || true")
            log_command_output("keymasqd journal", "journalctl -b -u keymasqd.service --no-pager -n 240 || true")
            log_command_output(
                "uinput permissions",
                "ls -l /dev/uinput || true; ${pkgs.acl}/bin/getfacl /dev/uinput || true",
            )
            log_command_output(
                "keymasq-session status",
                as_user("systemctl --user status keymasq-session.service --no-pager || true"),
            )
            log_command_output(
                "keymasq-session journal",
                as_user("journalctl --user -u keymasq-session.service --no-pager -n 240 || true"),
            )
            log_command_output("input devices", "cat /proc/bus/input/devices || true")
            log_command_output(
                "recording leases",
                "ls -la /run/keymasq /etc/keymasq || true; "
                + "cat /run/keymasq/macro-recording-enabled-${toString vmUid} "
                + "/etc/keymasq/macro-recording-enabled-${toString vmUid} 2>/dev/null || true",
            )
            log_command_output(
                "generated config",
                as_user("find ~/.config/keymasq -maxdepth 3 -type f -print -exec sed -n 1,220p {} \\; || true"),
            )
            log_command_output("integration test output", f"cat {output_path} || true")

        def wait_for_command(label: str, command: str, timeout: int = 60) -> None:
            deadline = time.time() + timeout
            while time.time() < deadline:
                rc = machine.execute(command)[0]
                if rc == 0:
                    return
                time.sleep(0.5)
            dump_debug(f"timed out waiting for {label}")
            raise Exception(f"Timed out waiting for {label}: {command}")

        def wait_for_user_command(label: str, command: str, timeout: int = 60) -> None:
            wait_for_command(label, as_user(command), timeout=timeout)

        start_all()
        machine.wait_for_unit("multi-user.target")
        machine.wait_for_unit("keymasqd.service")

        machine.succeed("modprobe uinput")
        wait_for_command("uinput device", "test -c /dev/uinput")
        machine.succeed("chgrp input /dev/uinput")
        machine.succeed("chmod g+rw /dev/uinput")
        machine.succeed("${pkgs.acl}/bin/setfacl -m u:${vmUser}:rw /dev/uinput")
        wait_for_user_command("uinput writable", "test -w /dev/uinput")

        machine.succeed("${keymasqPackage}/bin/keymasq-record enable-macro-recording-persistent --uid ${toString vmUid}")

        machine.succeed("loginctl enable-linger ${vmUser}")
        machine.wait_for_unit("user@${toString vmUid}.service")
        wait_for_command("runtime dir", "test -d ${runtimeDir}")
        wait_for_command("user bus", "test -S ${runtimeDir}/bus")

        machine.succeed(as_user("systemctl --user start keymasq-session.service"))
        wait_for_user_command(
            "keymasq-session",
            "systemctl --user is-active keymasq-session.service",
        )
        wait_for_command(
            "session socket",
            "test -S ${runtimeDir}/keymasq/session.sock",
        )

        status_code, runner_output = machine.execute(
            as_user(
                f"rm -f {output_path} {status_path}; "
                "set +e; "
                "${scenarioEnv}keymasq-daemon-session-integration-test "
                f"> {output_path} 2>&1; "
                "status=$?; "
                f"echo \"$status\" > {status_path}; "
                "exit 0"
            ),
            timeout=300,
        )
        if status_code != 0:
            raise Exception(f"failed to launch integration runner: {runner_output}")

        output = machine.succeed(f"cat {output_path} || true")
        print(output)
        status = int(machine.succeed(f"cat {status_path}").strip())
        if status != 0:
            dump_debug("daemon/session integration test failed")
            raise Exception("daemon-session-integration-test failed")
      '';
    };
in
{
  checks = {
    daemon-session-integration-test = mkDaemonSessionIntegrationTest {
      name = "daemon-session-integration-test";
      unlockRequired = false;
    };

    daemon-session-macro-slot-locked-playback-test = mkDaemonSessionIntegrationTest {
      name = "daemon-session-macro-slot-locked-playback-test";
      unlockRequired = true;
      scenarioFilter = "mapped-macro-slot-playback-without-capture-unlock";
    };
  };
}
