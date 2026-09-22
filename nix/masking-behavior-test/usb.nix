{ keymasqPackage, testPython }:
{
  exporter = { config, ... }: {
    virtualisation = { graphics = false; memorySize = 1024; cores = 2; };
    boot.kernelModules = [ "libcomposite" "usbip-vudc" ];
    boot.extraModprobeConfig = "options usbip-vudc num=4";
    networking.firewall.allowedTCPPorts = [ 3240 ];
    environment.systemPackages = [ config.boot.kernelPackages.usbip ];
    systemd.services.usb-gadgets = {
      wantedBy = [ "multi-user.target" ];
      after = [ "systemd-modules-load.service" "sys-kernel-config.mount" ];
      requires = [ "sys-kernel-config.mount" ];
      environment.PYTHONPATH = "${../daemon-session-integration-test}";
      serviceConfig.ExecStart = "${testPython}/bin/python ${./usb_gadget.py}";
    };
    systemd.services.usb-export = {
      wantedBy = [ "multi-user.target" ];
      after = [ "usb-gadgets.service" ];
      requires = [ "usb-gadgets.service" ];
      serviceConfig.ExecStart = "${config.boot.kernelPackages.usbip}/bin/usbipd --device";
    };
  };

  client = { config, ... }: {
    boot.kernelModules = [ "vhci-hcd" "joydev" ];
    environment.systemPackages = [ config.boot.kernelPackages.usbip ];
    services.udev.extraRules = ''
      SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="cafe", ATTR{idProduct}=="000[56]", GROUP="input", MODE="0660"
      SUBSYSTEM=="hidraw", ATTRS{idVendor}=="cafe", ATTRS{idProduct}=="000[56]", GROUP="input", MODE="0660"
      # USB/IP's platform ancestry otherwise gives these fixtures colliding platform-noserial aliases.
      SUBSYSTEM=="input", KERNEL=="event*", ATTRS{idVendor}=="cafe", ATTRS{idProduct}=="000[56]", ENV{ID_INPUT_JOYSTICK}=="1", SYMLINK:="input/by-id/usb-$attr{serial}-event-joystick"
      SUBSYSTEM=="input", KERNEL=="event*", ATTRS{idVendor}=="cafe", ATTRS{idProduct}=="000[56]", ENV{ID_INPUT_KEYBOARD}=="1", SYMLINK:="input/by-id/usb-$attr{serial}-event-kbd"
    '';
  };

  testScript = ''
    import re

    def usb_command(command):
        return user(
            "env KEYMASQ_MASK_TEST_NAMES=mask-usb-target,mask-usb-bystander "
            "${testPython}/bin/python ${./usb_check.py} " + command
        )

    def usb_check(command, unlock=False):
        if unlock:
            machine.succeed("${keymasqPackage}/bin/keymasq-record unlock-runtime --uid 1000 --ttl 120")
        return machine.succeed(usb_command(command), timeout=120)

    def usb_import(index):
        exporter.wait_until_succeeds(
            f"test $(cat /sys/devices/platform/usbip-vudc.{index}/usbip_status) -eq 1",
            timeout=15,
        )
        # USB/IP imports can race exporter teardown. Retry transport setup;
        # enumeration and masking assertions below must then pass independently.
        machine.wait_until_succeeds(
            f"usbip attach --remote exporter --busid usbip-vudc.{index}", timeout=15
        )
        machine.succeed("udevadm settle")

    def usb_attach(index):
        usb_import(index)
        usb_check(f"connected-{index}")

    def usb_detach(port=0):
        status = machine.succeed("cat /sys/devices/platform/vhci_hcd.0/status")
        rows = (line.split() for line in status.splitlines()[1:])
        busid = next(row[-1] for row in rows if int(row[1]) == port)
        assert re.fullmatch(r"[0-9]+-[0-9]+(?:\.[0-9]+)*", busid), status
        machine.succeed(f"usbip detach --port {port}")
        # detach only queues a kernel disconnect. Reusing the port before the
        # USB device disappears can send old URBs into the next attachment.
        machine.wait_until_succeeds(f"test ! -e /sys/bus/usb/devices/{busid}", timeout=15)
        machine.succeed("udevadm settle")

    def run_usb_tests():
        exporter.wait_for_unit("usb-export.service")
        exporter.wait_until_succeeds("test -e /run/keymasq-usb-gadgets-ready", timeout=30)
        machine.wait_until_succeeds("usbip list --remote exporter | grep usbip-vudc.2")
        usb_attach(0)
        usb_attach(1)
        usb_check("baseline")
        with subtest("USB disconnect revokes evdev, hidraw, and raw USB handles"):
            machine.succeed(usb_command("hold-physical") + " > /tmp/usb-physical-holder.log 2>&1 &")
            machine.wait_until_succeeds("test -e /tmp/keymasq-usb-physical-held")
            usb_detach()
            machine.wait_until_succeeds("test -e /tmp/keymasq-usb-physical-revoked")
            usb_attach(0)
            usb_check("baseline")
        with subtest("USB masking denies physical access, revokes HID handles, and forwards input"):
            usb_check("mask", unlock=True)
        with subtest("USB disconnect and repeated reconnect reapply the saved mask"):
            for iteration in range(3):
                machine.succeed(usb_command("hold-output") + " > /tmp/usb-output-holder.log 2>&1 &")
                machine.wait_until_succeeds("test -e /tmp/keymasq-usb-output-held")
                usb_detach()
                usb_check("disconnected")
                machine.wait_until_succeeds("test -e /tmp/keymasq-usb-output-revoked")
                machine.succeed("rm /tmp/keymasq-usb-output-held /tmp/keymasq-usb-output-revoked")
                usb_attach(0)
                usb_check("masked")
        with subtest("another USB device on the same port cannot inherit the mask"):
            usb_detach()
            usb_check("disconnected")
            usb_attach(2)
            usb_check("replacement")
            with subtest("moving the original USB device to another port does not inherit the mask"):
                usb_attach(0)
                usb_check("moved")
                usb_detach(2)
            usb_detach()
            usb_attach(0)
            usb_check("masked")
        with subtest("disabling a saved USB mask while absent restores access on return"):
            usb_detach()
            usb_check("disconnected")
            usb_check("disable-absent", unlock=True)
            usb_attach(0)
            usb_check("restored")
        usb_detach()
        with subtest("removing a partially adopted composite mask preserves shared mapped outputs"):
            def shared(command):
                return user(
                    "env KEYMASQ_MASK_TEST_NAMES=mask-usb-composite,mask-usb-bystander "
                    "${testPython}/bin/python ${./shared_outputs.py} " + command
                )
            usb_import(3)
            machine.succeed("${keymasqPackage}/bin/keymasq-record unlock-runtime --uid 1000 --ttl 120")
            machine.succeed(shared("setup"), timeout=120)
            machine.succeed(shared("hold") + " > /tmp/usb-shared-output.log 2>&1 &")
            machine.wait_until_succeeds("test -e /tmp/keymasq-shared-held", timeout=45)
            usb_detach()
            machine.wait_until_succeeds("test -e /tmp/keymasq-shared-absent-ok", timeout=40)
            usb_import(3)
            machine.wait_until_succeeds("test -e /tmp/keymasq-shared-returned-ok", timeout=40)
            machine.succeed("${keymasqPackage}/bin/keymasq-record unlock-runtime --uid 1000 --ttl 120")
            machine.succeed(shared("cleanup"), timeout=120)
            usb_detach()
            usb_detach(1)
  '';
}
