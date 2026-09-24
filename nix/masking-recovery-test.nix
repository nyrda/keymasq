# Run from the repository root: ./scripts/integration.sh masking-recovery
# Uses kernel UHID devices to check cleanup after SIGKILL, watchdog expiry,
# interrupted activation, service stop, package removal, clean reboot, and
# abrupt power loss.
{ pkgs, keymasqPackage, keymasqModule }:

let
  vmUser = "masktest";
  runtimeDir = "/run/user/1000";
  common = import ./masking-test-common.nix { inherit pkgs keymasqPackage keymasqModule; };
  inherit (common) testPython control pythonPath;
  holdJob = pkgs.writeShellScript "hold-completed-hardware-job" ''
    if test -e /run/keymasq-test-hold-job; then
      ${pkgs.coreutils}/bin/touch /run/keymasq-masking/test-job-held
      exec ${pkgs.coreutils}/bin/sleep infinity
    fi
  '';
in
pkgs.testers.runNixOSTest {
  name = "masking-recovery-test";

  nodes.machine = { lib, ... }: {
    imports = [ common.machine ];
    # Keep the daemon down until stop-time recovery has been independently
    # checked. The production ExecStartPre must not hide an ExecStopPost failure.
    systemd.services.keymasqd.serviceConfig.Restart = lib.mkForce "no";
    # Hold the real systemd job after hardware mutation but before start returns
    # to the daemon. This makes the interrupted-activation case deterministic.
    systemd.services."keymasq-hardware@".serviceConfig.ExecStartPost = [ holdJob ];
  };

  testScript = ''
    import json

    def user(command):
        return (
            "runuser -u ${vmUser} -- env "
            "XDG_RUNTIME_DIR=${runtimeDir} "
            "DBUS_SESSION_BUS_ADDRESS=unix:path=${runtimeDir}/bus "
            "PYTHONPATH=${pythonPath} " + command
        )

    def check(command):
        if command in ("mask", "mask-persistent", "start-mask"):
            machine.succeed("${keymasqPackage}/bin/keymasq-record unlock-runtime --uid 1000 --ttl 120")
        return machine.succeed(user("${testPython}/bin/python ${control} " + command), timeout=90)

    def ready():
        machine.wait_for_unit("keymasq-test-devices.service")
        machine.wait_for_unit("keymasqd.service")
        machine.succeed("loginctl enable-linger ${vmUser}")
        machine.wait_for_unit("user@1000.service")
        machine.succeed(user("systemctl --user start keymasq-session.service"))
        machine.wait_until_succeeds("test -S ${runtimeDir}/keymasq/session.sock", timeout=30)
        machine.succeed("udevadm settle")
        check("ready")

    def stopped(result):
        machine.wait_until_succeeds("systemctl is-failed --quiet keymasqd.service", timeout=45)
        actual = machine.succeed("systemctl show keymasqd.service -p Result --value").strip()
        assert actual == result, actual
        assert machine.succeed("systemctl show keymasqd.service -p MainPID --value").strip() == "0"
        check("restored")
        jobs = json.loads(machine.succeed(
            "systemctl list-units 'keymasq-hardware@*.service' --state=active,activating,deactivating --output=json"
        ))
        assert not jobs, jobs

    def restart():
        machine.succeed("systemctl reset-failed keymasqd.service")
        machine.succeed("systemctl start keymasqd.service")
        ready()
        check("restored")

    start_all()
    try:
        ready()
        check("baseline")

        with subtest("SIGKILL of a daemon with a confirmed mask"):
            check("mask")
            machine.succeed("systemctl kill --kill-whom=main --signal=SIGKILL keymasqd.service")
            stopped("signal")
            restart()

        with subtest("watchdog kills a stopped daemon and restores its confirmed mask"):
            check("mask")
            machine.succeed("systemctl kill --kill-whom=main --signal=SIGSTOP keymasqd.service")
            stopped("watchdog")
            restart()

        with subtest("SIGKILL during activation stops the outstanding hardware job"):
            machine.succeed("touch /run/keymasq-test-hold-job")
            check("start-mask")
            machine.wait_until_succeeds("test -f /run/keymasq-masking/test-job-held", timeout=30)
            check("masked")
            jobs = json.loads(machine.succeed(
                "systemctl list-units 'keymasq-hardware@*.service' --state=activating --output=json"
            ))
            assert len(jobs) == 1 and jobs[0]["sub"] == "start-post", jobs
            machine.succeed("systemctl kill --kill-whom=main --signal=SIGKILL keymasqd.service")
            stopped("signal")
            machine.succeed("rm /run/keymasq-test-hold-job /run/keymasq-masking/test-job-held")
            restart()

        with subtest("clean service stop restores a confirmed mask"):
            check("mask")
            machine.succeed("systemctl stop keymasqd.service")
            check("restored")
            restart()

        with subtest("package removal waits for hardware recovery it cannot finish"):
            check("mask")
            # Every hardware job and recovery takes this lock without waiting.
            lock = "${pkgs.util-linux}/bin/flock -n -s /run/keymasq-masking/operations.lock true"
            machine.succeed(
                "systemd-run --unit=keymasq-test-recovery-lock "
                "${pkgs.util-linux}/bin/flock -x /run/keymasq-masking/operations.lock "
                "${pkgs.coreutils}/bin/sleep infinity"
            )
            machine.wait_until_fails(lock, timeout=15)
            status, output = machine.execute(
                "${keymasqPackage}/bin/keymasq-record prepare-removal 2>&1"
            )
            assert status != 0, output
            assert "cannot be removed yet" in output, output
            assert machine.succeed(
                "systemctl show keymasqd.service -p MainPID --value"
            ).strip() == "0"
            check("restricted")
            machine.succeed("systemctl stop keymasq-test-recovery-lock.service")
            machine.succeed("${keymasqPackage}/bin/keymasq-record prepare-removal")
            check("removed")
            restart()

        with subtest("clean shutdown and next boot restore a nonpersistent mask"):
            check("mask")
            machine.shutdown()
            machine.start()
            ready()
            check("restored")

        with subtest("power loss and next boot restore a nonpersistent mask"):
            check("mask")
            machine.crash()
            machine.start()
            ready()
            check("restored")

        with subtest("persistent mask is cleaned on SIGKILL and reapplied after restart"):
            check("mask-persistent")
            machine.succeed("systemctl kill --kill-whom=main --signal=SIGKILL keymasqd.service")
            stopped("signal")
            machine.succeed("systemctl reset-failed keymasqd.service")
            machine.succeed("systemctl start keymasqd.service")
            ready()
            check("wait-masked")
            check("restore")
            check("restored")
    except Exception:
        print(machine.execute("journalctl -b -u keymasqd.service -u 'keymasq-hardware@*' --no-pager -n 180")[1])
        print(machine.execute("journalctl -b _UID=1000 --no-pager -n 80")[1])
        print(machine.execute("ls -laR /run/keymasq-masking /run/udev/rules.d")[1])
        raise
  '';
}
