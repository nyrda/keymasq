# Run from the repository root: ./scripts/integration.sh masking-behavior
# Uses kernel UHID devices to check trials, independent masks, reconnects,
# remapping, saved choices across reboot, and rejected requests.
{ pkgs, keymasqPackage, keymasqModule }:

let
  common = import ./masking-test-common.nix { inherit pkgs keymasqPackage keymasqModule; };
  inherit (common) testPython control pythonPath;
  behavior = ./masking-behavior-test/behavior.py;
  usb = import ./masking-behavior-test/usb.nix { inherit keymasqPackage testPython; };
in
pkgs.testers.runNixOSTest {
  name = "masking-behavior-test";

  nodes.machine = { lib, ... }: {
    imports = [ common.machine usb.client ];
    systemd.services.keymasq-test-devices.serviceConfig.ExecStart = lib.mkForce
      "${testPython}/bin/python ${./masking-behavior-test/fixture.py}";
  };
  nodes.exporter = usb.exporter;

  testScript = ''
    def user(command):
        return (
            "runuser -u masktest -- env "
            "XDG_RUNTIME_DIR=/run/user/1000 "
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "
            "PYTHONPATH=${pythonPath}:${./masking-behavior-test} " + command
        )

    def control(command):
        return machine.succeed(user("${testPython}/bin/python ${control} " + command), timeout=90)

    def check(command, unlock=False):
        if unlock:
            machine.succeed("${keymasqPackage}/bin/keymasq-helper unlock-runtime --uid 1000 --ttl 120")
        return machine.succeed(user("${testPython}/bin/python ${behavior} " + command), timeout=120)

    def ready():
        machine.wait_for_unit("keymasq-test-devices.service")
        machine.wait_for_unit("keymasqd.service")
        machine.wait_until_succeeds("test -S /run/keymasq-test-devices.sock", timeout=30)
        machine.succeed("loginctl enable-linger masktest")
        machine.wait_for_unit("user@1000.service")
        machine.succeed(user("systemctl --user start keymasq-session.service"))
        machine.wait_until_succeeds("test -S /run/user/1000/keymasq/session.sock", timeout=30)
        machine.succeed("udevadm settle")
        control("ready")

    def restart_and_check_off():
        machine.succeed("systemctl restart keymasqd.service")
        ready()
        check("off")

    ${usb.testScript}

    start_all()
    try:
        ready()
        control("baseline")
        run_usb_tests()
        with subtest("locked masking request leaves device access unchanged"):
            check("locked")
        with subtest("unconfirmed trial expires and stays off after restart"):
            check("trial-expiry", unlock=True)
            restart_and_check_off()
        with subtest("explicit undo restores a trial and stays off after restart"):
            check("undo", unlock=True)
            restart_and_check_off()
        with subtest("closing the GUI does not cancel trial expiry"):
            check("trial-close", unlock=True)
            restart_and_check_off()
        with subtest("two masks restore independently and unmask all preserves ordinary remapping"):
            check("multiple", unlock=True)
            restart_and_check_off()
        with subtest("confirmed reconnect preserves identity and the other active mask"):
            check("reconnect", unlock=True)
        with subtest("masked profile changes preserve press-release pairing and return to passthrough"):
            check("remapping", unlock=True)
        with subtest("wrong-device and stale tokens cannot mutate masks"):
            check("tokens", unlock=True)
        with subtest("saved mask survives GUI close and starts after reboot without a GUI"):
            check("saved-on", unlock=True)
            machine.shutdown()
            machine.start()
            ready()
            check("saved-after-boot")
        with subtest("switching off a saved mask keeps it off after another reboot"):
            check("saved-off")
            machine.shutdown()
            machine.start()
            ready()
            check("off")
    except Exception:
        print(exporter.execute("journalctl -b -u usb-gadgets -u usb-export --no-pager -n 100")[1])
        print(machine.execute("usbip port; dmesg | tail -n 60")[1])
        print(machine.execute("cat /tmp/usb-*-holder.log /tmp/usb-shared-output.log")[1])
        print(machine.execute("journalctl -b -u keymasqd.service -u 'keymasq-hardware@*' --no-pager -n 180")[1])
        print(machine.execute("journalctl -b _UID=1000 --no-pager -n 100")[1])
        print(machine.execute("journalctl -b -u keymasq-test-devices --no-pager -n 60")[1])
        print(machine.execute("ls -laR /run/keymasq-masking /run/udev/rules.d")[1])
        raise
  '';
}
