{
  pkgs,
  system,
  keymasqPackage,
  keymasqModule,
  checkSuffix ? "",
  evdevPackage ? pkgs.python3Packages.evdev,
}:

let
  vmUser = "keymasqvm";
  vmUid = 1000;
  runtimeDir = "/run/user/${toString vmUid}";
  testSource = ./daemon-session-integration-test;
  testPython = pkgs.python3.withPackages (ps: [ evdevPackage ps.fusepy ]);
  tomlFormat = pkgs.formats.toml { };
  vmSecurityConfig = {
    daemon_allowed_uids = [ vmUid ];
    session_allowed_uids = [ vmUid ];
    recording_guard.macro_recording_allowed = true;
  };
  recordingDisabledSecurityToml = tomlFormat.generate "keymasq-security-recording-disabled.toml" (
    pkgs.lib.recursiveUpdate vmSecurityConfig {
      recording_guard.macro_recording_allowed = false;
    }
  );

  hidBpfProbe = pkgs.writeShellScript "keymasq-hid-bpf-probe" ''
    exec ${testPython}/bin/python ${testSource}/hid_bpf_probe.py ${pkgs.systemd}/bin/systemctl "$@"
  '';
  bpftool = "${pkgs.bpftools}/bin/bpftool";

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
      export KEYMASQ_INTEGRATION_BPFTOOL="${bpftool}"
      export KEYMASQ_INTEGRATION_HID_BPF_PROBE="${hidBpfProbe}"
      exec ${testPython}/bin/python ${testSource}/runner.py "$@"
    '';
  };
  validScenarioFilter =
    value:
    value == "" || (builtins.match "[A-Za-z0-9_.-]+(,[A-Za-z0-9_.-]+)*" value) != null;
  validRepeatCount =
    value:
    value == "" || (builtins.match "[1-9][0-9]*" value) != null;
  selectedScenarioFilter = builtins.getEnv "KEYMASQ_INTEGRATION_SCENARIOS";
  selectedRepeatCount = builtins.getEnv "KEYMASQ_INTEGRATION_REPEAT";

  userCommand =
    cmd:
    "runuser -u ${vmUser} -- sh -lc 'export HOME=/home/${vmUser}; "
    + "export XDG_RUNTIME_DIR=${runtimeDir}; "
    + "export DBUS_SESSION_BUS_ADDRESS=unix:path=${runtimeDir}/bus; "
    + "${cmd}'";
  mkDaemonSessionIntegrationTest =
    {
      name,
      scenarioFilter ? "",
      repeatCount ? "",
    }:
    let
      checkedScenarioFilter =
        assert validScenarioFilter scenarioFilter;
        scenarioFilter;
      checkedRepeatCount =
        assert validRepeatCount repeatCount;
        repeatCount;
      scenarioEnv =
        if checkedScenarioFilter == "" then
          ""
        else
          "KEYMASQ_INTEGRATION_SCENARIOS=${checkedScenarioFilter} ";
      repeatEnv =
        if checkedRepeatCount == "" then
          ""
        else
          "KEYMASQ_INTEGRATION_REPEAT=${checkedRepeatCount} ";
      repeatMultiplier =
        if checkedRepeatCount == "" then
          1
        else
          pkgs.lib.toInt checkedRepeatCount;
      runnerTimeout = 300 * repeatMultiplier;
      runPolicyDisabledPhase = checkedScenarioFilter == "";
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

          boot.kernelModules = [ "uinput" "uhid" "fuse" "hid_steam" ];
          boot.extraModprobeConfig = "options hid_steam lizard_mode=0";
          programs.fuse.enable = true;
          # Only the test client creates emulated physical HID devices. The
          # daemon keeps the production ACLs and device cgroup restrictions.
          services.udev.extraRules = ''
            SUBSYSTEM=="misc", KERNEL=="uhid", OWNER="${vmUser}", MODE="0600"
          '';

          services.keymasq = {
            enable = true;
            package = keymasqPackage;
            securityConfig = vmSecurityConfig;
          };

          systemd.services.keymasqd.serviceConfig.Environment = [ "KEYMASQ_TEST_UINPUT=1" ];

          users.users.${vmUser} = {
            isNormalUser = true;
            uid = vmUid;
            description = "Daemon/session integration test VM user";
            createHome = true;
            home = "/home/${vmUser}";
            extraGroups = [ "input" "systemd-journal" ];
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
                      command = "${bpftool} --json struct_ops show";
                      options = [ "NOPASSWD" ];
                    }
                  ];
                }
                {
                  users = [ vmUser ];
                  runAs = "keymasq";
                  commands = [
                    {
                      command = "${hidBpfProbe}";
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
                "hardware jobs journal",
                "journalctl -b -u 'keymasq-hardware@*' --no-pager -n 120 || true",
            )
            log_command_output(
                "HID devices and struct_ops maps",
                "dmesg | grep -i -e hid -e steam -e uhid | tail -60; "
                + "${bpftool} struct_ops show || true",
            )
            log_command_output(
                "udev rule simulation for the newest event node",
                "n=$(ls /sys/class/input | grep '^event' | sort -V | tail -1); "
                + "ls -l /dev/input/$n; ls -la /run/keymasq/hidden /run/keymasq/hidden-hardware; "
                + "udevadm test --action=change /sys/class/input/$n 2>&1 | tail -80 || true",
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

        # A document-portal-style mount rejects even root's stat of an open
        # descriptor. Run the real mount and a detached-namespace refusal case.
        machine.succeed("install -d -o ${vmUser} -m 0700 /run/keymasq-fuse-test /run/keymasq-fuse-test/mount")
        machine.succeed(
            "systemd-run --unit=keymasq-test-fuse --uid=${vmUser} "
            "--property=RemainAfterExit=yes "
            "--setenv=FUSE_LIBRARY_PATH=${pkgs.lib.getLib pkgs.fuse}/lib/libfuse.so.2 "
            "--setenv=PATH=/run/wrappers/bin:${pkgs.fuse}/bin:${pkgs.coreutils}/bin "
            "${testPython}/bin/python ${testSource}/fuse_scan.py serve /run/keymasq-fuse-test/mount"
        )
        # mountpoint stats the directory, which this private mount denies root.
        wait_for_command(
            "FUSE mount",
            "runuser -u ${vmUser} -- mountpoint -q /run/keymasq-fuse-test/mount",
            timeout=10,
        )
        try:
            machine.succeed(
                "PYTHONPATH=${keymasqPackage}/${pkgs.python3.sitePackages} "
                "${testPython}/bin/python ${testSource}/fuse_scan.py check ${vmUser} /run/keymasq-fuse-test/mount"
            )
        finally:
            machine.succeed("umount /run/keymasq-fuse-test/mount")
            machine.succeed("systemctl stop keymasq-test-fuse.service")

        machine.succeed("modprobe uinput")
        wait_for_command("uinput device", "test -c /dev/uinput")
        machine.succeed("chgrp input /dev/uinput")
        machine.succeed("chmod g+rw /dev/uinput")
        machine.succeed("${pkgs.acl}/bin/setfacl -m u:${vmUser}:rw /dev/uinput")
        wait_for_user_command("uinput writable", "test -w /dev/uinput")

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

        def run_integration_runner(env: str, timeout: int, label: str) -> None:
            status_code, runner_output = machine.execute(
                as_user(
                    f"rm -f {output_path} {status_path}; "
                    "set +e; "
                    f"{env}keymasq-daemon-session-integration-test "
                    f"> {output_path} 2>&1; "
                    "status=$?; "
                    f"echo \"$status\" > {status_path}; "
                    "exit 0"
                ),
                timeout=timeout,
            )
            if status_code != 0:
                raise Exception(f"failed to launch integration runner: {runner_output}")

            output = machine.succeed(f"cat {output_path} || true")
            print(output)
            status = int(machine.succeed(f"cat {status_path}").strip())
            if status != 0:
                dump_debug(f"{label} failed")
                raise Exception(f"{label} failed")

        run_integration_runner(
            "${scenarioEnv}${repeatEnv}",
            ${toString runnerTimeout},
            "daemon-session-integration-test",
        )

        run_policy_disabled_phase = ${if runPolicyDisabledPhase then "True" else "False"}
        if run_policy_disabled_phase:
            # Both services read the policy only at startup.
            machine.succeed(
                "rm /etc/keymasq/security.toml && "
                "install -m 0644 ${recordingDisabledSecurityToml} /etc/keymasq/security.toml"
            )
            machine.succeed("systemctl restart keymasqd.service")
            machine.wait_for_unit("keymasqd.service")
            machine.succeed(as_user("systemctl --user restart keymasq-session.service"))
            wait_for_user_command(
                "keymasq-session",
                "systemctl --user is-active keymasq-session.service",
            )
            wait_for_command(
                "session socket",
                "test -S ${runtimeDir}/keymasq/session.sock",
            )
            run_integration_runner(
                "KEYMASQ_INTEGRATION_SCENARIOS=macro-recording-disabled-by-policy ",
                300,
                "daemon-session policy-disabled phase",
            )
      '';
    };
in
{
  checks = {
    "daemon-session-integration-test${checkSuffix}" = mkDaemonSessionIntegrationTest {
      name = "daemon-session-integration-test${checkSuffix}";
    };

    "daemon-session-selected-integration-test${checkSuffix}" = mkDaemonSessionIntegrationTest {
      name = "daemon-session-selected-integration-test${checkSuffix}";
      scenarioFilter = selectedScenarioFilter;
      repeatCount = selectedRepeatCount;
    };
  };
}
