{
  pkgs,
  appimageArtifact,
}:

let
  deckUser = "deck";
  deckUid = 1000;
  runtimeDir = "/run/user/${toString deckUid}";
  browserPython = pkgs.python3.withPackages (ps: [
    ps.pillow
    ps.selenium
  ]);
  browserProbe = pkgs.writeShellApplication {
    name = "keymasq-appimage-brotway-browser-probe";
    runtimeInputs = [ browserPython ];
    text = ''
      export KEYMASQ_CHROMIUM=${pkgs.chromium}/bin/chromium
      export KEYMASQ_CHROMEDRIVER=${pkgs.chromedriver}/bin/chromedriver
      exec ${browserPython}/bin/python ${./appimage-brotway-integration-test/browser_probe.py} "$@"
    '';
  };
  iconGallery = pkgs.runCommand "keymasq-appimage-icon-gallery" { } ''
    install -Dm644 ${./appimage-brotway-integration-test/icon_gallery.py} $out/icon_gallery.py
    install -Dm755 ${./appimage-brotway-integration-test/run_icon_gallery.sh} $out/run_icon_gallery.sh
  '';
  iconGalleryBrowserProbe = pkgs.writeShellApplication {
    name = "keymasq-appimage-icon-gallery-browser-probe";
    runtimeInputs = [ browserPython ];
    text = ''
      export KEYMASQ_CHROMIUM=${pkgs.chromium}/bin/chromium
      export KEYMASQ_CHROMEDRIVER=${pkgs.chromedriver}/bin/chromedriver
      export PYTHONPATH=${./appimage-brotway-integration-test}
      exec ${browserPython}/bin/python ${./appimage-brotway-integration-test/icon_gallery_probe.py} "$@"
    '';
  };
  expectedIconNames = pkgs.lib.filter (name: name != "") (
    pkgs.lib.splitString "\n" (builtins.readFile ../packaging/appimage/assets/gui-icon-names.txt)
  );
in
pkgs.testers.runNixOSTest {
  name = "appimage-brotway-integration-test";

  nodes = {
    deck =
      { ... }:
      {
        documentation.nixos.enable = false;

        networking = {
          hostName = "deck";
          firewall.allowedTCPPorts = [
            18101
            18102
            18103
          ];
        };
        time.timeZone = "UTC";
        i18n.defaultLocale = "en_US.UTF-8";

        virtualisation = {
          graphics = false;
          memorySize = 3072;
          cores = 2;
        };

        boot.kernelModules = [
          "fuse"
          "uinput"
        ];

        users.users.${deckUser} = {
          isNormalUser = true;
          uid = deckUid;
          description = "Steam Deck AppImage integration test user";
          createHome = true;
          home = "/home/${deckUser}";
          extraGroups = [ "input" ];
        };

        services.dbus.enable = true;
        security.polkit.enable = true;
        services.udev.enable = true;

        environment.systemPackages = [
          pkgs.acl
          pkgs.coreutils
          pkgs.curl
          pkgs.fuse3
          pkgs.iproute2
          pkgs.procps
          pkgs.systemd
          pkgs.util-linux
        ];

        # The AppImage intentionally installs distribution-neutral units whose
        # ExecStartPre paths follow the /usr/bin layout present on SteamOS.
        # Supply those host tools without installing Keymasq from Nix.
        system.activationScripts.keymasqAppimageHostCompatibility.text = ''
          materialize_etc_tree() {
            path=$1
            if [ -L "$path" ]; then
              staged=$(mktemp -d)
              cp -a "$path/." "$staged/"
              rm "$path"
              mv "$staged" "$path"
            else
              install -d -m 0755 "$path"
            fi
          }

          # NixOS generates these as immutable /etc/static symlink trees.
          # SteamOS and the other AppImage targets expose writable directories,
          # so materialize their current contents in this disposable VM.
          materialize_etc_tree /etc/systemd/system
          materialize_etc_tree /etc/udev/rules.d

          install -d -m 0755 /bin
          ln -sfn ${pkgs.util-linux}/bin/mount /bin/mount
          ln -sfn ${pkgs.util-linux}/bin/umount /bin/umount
          install -d -m 0755 /usr/bin
          ln -sfn ${pkgs.bash}/bin/sh /usr/bin/sh
          ln -sfn ${pkgs.acl}/bin/setfacl /usr/bin/setfacl
          ln -sfn ${pkgs.coreutils}/bin/chmod /usr/bin/chmod
          ln -sfn ${pkgs.systemd}/bin/udevadm /usr/bin/udevadm
        '';
      };

    browser =
      { ... }:
      {
        documentation.nixos.enable = false;
        networking.hostName = "browser";
        time.timeZone = "UTC";

        virtualisation = {
          graphics = false;
          memorySize = 4096;
          cores = 2;
        };

        environment.systemPackages = [
          browserProbe
          iconGalleryBrowserProbe
          pkgs.chromium
          pkgs.chromedriver
          pkgs.curl
          browserPython
        ];
      };
  };

  testScript = ''
    import json
    import shlex
    import time

    artifact = "${appimageArtifact}"
    runtime = "/opt/keymasq/runtime/current"
    browser_output = "/tmp/keymasq-brotway"
    gallery_browser_output = "/tmp/keymasq-icon-gallery"
    gallery_result_path = "/home/${deckUser}/keymasq-icon-gallery-result.json"
    expected_icon_names = ${builtins.toJSON expectedIconNames}
    expected_assets = ["gamepad"]

    def as_deck(command: str) -> str:
        environment = (
            "HOME=/home/${deckUser} "
            "XDG_RUNTIME_DIR=${runtimeDir} "
            "DBUS_SESSION_BUS_ADDRESS=unix:path=${runtimeDir}/bus "
        )
        return "runuser -u ${deckUser} -- env " + environment + "sh -lc " + shlex.quote(command)

    def log_command(machine, label: str, command: str) -> None:
        status, output = machine.execute(command)
        machine.log(f"{label} (exit={status})\n{output}")

    def dump_diagnostics(label: str) -> None:
        deck.log(f"==== {label}: deck diagnostics ====")
        log_command(deck, "installed tree", "find /opt/keymasq -maxdepth 4 -printf '%M %u:%g %p -> %l\\n' 2>/dev/null | sort || true")
        log_command(deck, "artifact hashes", f"sha256sum {artifact} /tmp/Keymasq.AppImage /opt/keymasq/Keymasq.AppImage 2>/dev/null || true")
        log_command(deck, "runtime target", "readlink -v /opt/keymasq/runtime/current || true")
        log_command(deck, "processes", "ps auxww || true")
        log_command(deck, "sockets", "ss -ltnp; ss -lxnp || true")
        log_command(deck, "keymasqd status", "systemctl status keymasqd.service --no-pager || true")
        log_command(deck, "keymasqd journal", "journalctl -b -u keymasqd.service --no-pager -n 300 || true")
        log_command(deck, "session status", as_deck("systemctl --user status keymasq-session.service --no-pager || true"))
        log_command(deck, "session journal", as_deck("journalctl --user -u keymasq-session.service --no-pager -n 300 || true"))
        log_command(deck, "Brotway status", as_deck("systemctl --user status keymasq-brotway.service --no-pager || true"))
        log_command(deck, "Brotway journal", as_deck("journalctl --user -u keymasq-brotway.service --no-pager -n 400 || true"))
        log_command(deck, "icon gallery result", f"cat {gallery_result_path} 2>/dev/null || true")
        log_command(deck, "icon gallery journal", as_deck("journalctl --user -u keymasq-icon-gallery.service -u keymasq-icon-gallery-restart.service --no-pager -n 400 || true"))
        log_command(deck, "private GTK maps", "grep -l '/lib/gtk4-brotway/libgtk-4.so' /proc/[0-9]*/maps 2>/dev/null || true")
        log_command(deck, "private GTK cgroups", "for maps in /proc/[0-9]*/maps; do grep -q '/lib/gtk4-brotway/libgtk-4.so' \"$maps\" 2>/dev/null || continue; pid=''${maps#/proc/}; pid=''${pid%/maps}; echo PID=$pid; cat /proc/$pid/cgroup; done")
        browser.log(f"==== {label}: browser diagnostics ====")
        log_command(browser, "browser result", f"cat {browser_output}/brotway-browser-result.json 2>/dev/null || true")
        log_command(browser, "browser artifact files", f"sha256sum {browser_output}/* 2>/dev/null || true; ls -lh {browser_output} 2>/dev/null || true")
        log_command(browser, "browser probe journal", "journalctl -b -u keymasq-brotway-browser.service --no-pager -n 300 || true")
        log_command(browser, "icon gallery browser result", f"cat {gallery_browser_output}/icon-gallery-browser-result.json 2>/dev/null || true")
        log_command(browser, "icon gallery browser artifacts", f"sha256sum {gallery_browser_output}/* 2>/dev/null || true; ls -lh {gallery_browser_output} 2>/dev/null || true")
        log_command(browser, "icon gallery browser journal", "journalctl -b -u keymasq-icon-gallery-browser.service --no-pager -n 300 || true")
        log_command(browser, "Chromium processes", "ps auxww | grep -E '[c]hrom(e|ium)|[c]hromedriver' || true")

    def wait_for(machine, label: str, command: str, timeout: int = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if machine.execute(command)[0] == 0:
                return
            time.sleep(0.5)
        dump_diagnostics(f"timed out waiting for {label}")
        raise Exception(f"timed out waiting for {label}: {command}")

    start_all()
    deck.wait_for_unit("multi-user.target")
    browser.wait_for_unit("multi-user.target")

    try:
        deck.succeed("modprobe fuse; modprobe uinput")
        deck.succeed("test -c /dev/fuse; test -c /dev/uinput")
        deck.succeed("loginctl enable-linger ${deckUser}")
        deck.wait_for_unit("user@${toString deckUid}.service")
        wait_for(deck, "Deck user D-Bus", "test -S ${runtimeDir}/bus")

        # Execute the real Type-2 AppImage. APPIMAGE_EXTRACT_AND_RUN and the
        # installer's extracted-source test override are deliberately absent.
        deck.succeed(f"cp {artifact} /tmp/Keymasq.AppImage; chmod 0755 /tmp/Keymasq.AppImage")
        deck.succeed("test -z \"''${APPIMAGE_EXTRACT_AND_RUN:-}\"; test -z \"''${KEYMASQ_APPIMAGE_EXTRACTED_SOURCE_DIR:-}\"")
        install_status, install_output = deck.execute(
            "/tmp/Keymasq.AppImage --install --user ${deckUser}", timeout=240
        )
        deck.log(f"AppImage installer (exit={install_status})\n{install_output}")
        if install_status != 0:
            raise Exception("the actual AppImage installer failed")
        if "failed to utilize FUSE" in install_output or "Trying to extract and run" in install_output:
            raise Exception("the AppImage runtime fell back instead of mounting through FUSE")

        expected_hash = deck.succeed(f"sha256sum {artifact} | cut -d' ' -f1").strip()
        installed_hash = deck.succeed("sha256sum /opt/keymasq/Keymasq.AppImage | cut -d' ' -f1").strip()
        assert installed_hash == expected_hash, (expected_hash, installed_hash)
        assert deck.succeed("readlink /opt/keymasq/runtime/current").strip() == expected_hash
        deck.succeed(f"test -d /opt/keymasq/runtime/{expected_hash}")
        deck.succeed("test -x /opt/keymasq/bin/gtk4-brotway-run")
        deck.succeed("test -x /home/${deckUser}/.local/bin/gtk4-brotway-run")
        deck.succeed(f"test -x {runtime}/bin/gtk4-brotway-run")
        deck.succeed(f"test -x {runtime}/lib/gtk4-brotway/gtk4-broadwayd")
        deck.succeed(f"test -x {runtime}/lib/gtk4-brotway/gtk4-brotway-debugmenu")
        deck.succeed(f"test -e {runtime}/lib/gtk4-brotway/libgtk-4.so.1")

        deck.wait_for_unit("keymasqd.service")
        wait_for(
            deck,
            "installed user session service",
            as_deck("systemctl --user is-active --quiet keymasq-session.service"),
        )
        wait_for(deck, "session socket", "test -S ${runtimeDir}/keymasq/session.sock")
        status_output = deck.succeed(as_deck("/opt/keymasq/bin/keymasq --json status"))
        deck.log(f"installed CLI status:\n{status_output}")

        # The unauthenticated WebUI must not be network-reachable by default.
        # Port 18103 is deliberately open in the VM firewall so that the
        # browser-side failure proves the process itself is loopback-only.
        deck.succeed(
            as_deck(
                "systemd-run --user --unit=keymasq-brotway-loopback --collect "
                "-- /opt/keymasq/bin/gtk4-brotway-run --display :7 --port 18103"
            )
        )
        wait_for(
            deck,
            "loopback-only Brotway listener",
            "ss -ltnH | awk '$4 == \"127.0.0.1:18103\" { found=1 } END { exit !found }'",
            timeout=90,
        )
        browser.succeed("! curl --connect-timeout 2 -fsS http://deck:18103/ >/dev/null")
        deck.succeed(as_deck("systemctl --user stop keymasq-brotway-loopback.service"))

        deck.succeed(
            as_deck(
                "systemd-run --user --unit=keymasq-brotway --collect "
                "-- "
                "/opt/keymasq/bin/gtk4-brotway-run --address 0.0.0.0 "
                "--port 18101 /opt/keymasq/bin/keymasq"
            )
        )
        wait_for(deck, "Brotway TCP listener", "ss -ltn | grep -q ':18101 '", timeout=90)
        wait_for(browser, "Brotway HTTP page", "curl -fsS http://deck:18101/ >/dev/null", timeout=90)

        # A second launch on the same port must fail before GTK starts. The
        # bundled upstream launcher otherwise continues after its warning and
        # produces a misleading cascade of display-initialization failures.
        busy_status, busy_output = deck.execute(
            as_deck(
                "/opt/keymasq/bin/gtk4-brotway-run --address 0.0.0.0 "
                "--port 18101 /opt/keymasq/bin/keymasq 2>&1"
            )
        )
        deck.log(f"occupied-port launch (exit={busy_status}):\n{busy_output}")
        assert busy_status == 1, (busy_status, busy_output)
        assert busy_output.strip() == (
            "gtk4-brotway-run: port 18101 is already in use on 0.0.0.0"
        ), busy_output

        # Before the debug helper exists, identify the Python GUI itself rather
        # than accepting gtk4-broadwayd, which maps the same private GTK.
        gui_private_map_command = (
            "for maps in /proc/[0-9]*/maps; do "
            "grep -q '/lib/gtk4-brotway/libgtk-4.so' \"$maps\" 2>/dev/null || continue; "
            "pid=$(printf '%s' \"$maps\" | cut -d/ -f3); "
            "tr '\\0' ' ' < /proc/$pid/cmdline 2>/dev/null "
            "| grep -q -- ' -m keymasq ' || continue; "
            "printf '%s\\n' \"$maps\"; "
            "done"
        )
        wait_for(
            deck,
            "Keymasq GUI private GTK mapping",
            f"{gui_private_map_command} | grep -q .",
            timeout=90,
        )
        private_gui_map_paths = deck.succeed(gui_private_map_command).strip().splitlines()
        assert len(private_gui_map_paths) >= 1, private_gui_map_paths
        gui_pid = private_gui_map_paths[0].split("/")[2]
        gui_cgroup = deck.succeed(f"cat /proc/{gui_pid}/cgroup")
        assert "keymasq-brotway.service" in gui_cgroup, gui_cgroup
        deck.log(f"private GTK GUI pid={gui_pid}, cgroup={gui_cgroup.strip()}")

        browser.succeed(f"rm -rf {browser_output}; mkdir -p {browser_output}")
        browser.succeed(
            "systemd-run --unit=keymasq-brotway-browser --collect "
            f"--setenv=KEYMASQ_BROTWAY_OUTPUT_DIR={browser_output} "
            "--setenv=KEYMASQ_BROTWAY_HOLD_SECONDS=300 -- "
            "keymasq-appimage-brotway-browser-probe"
        )
        wait_for(
            browser,
            "Chromium probe result",
            f"test -s {browser_output}/brotway-browser-result.json || "
            "! systemctl is-active --quiet keymasq-brotway-browser.service",
            timeout=150,
        )
        browser_result = browser.succeed(
            f"test -s {browser_output}/brotway-browser-result.json; "
            f"cat {browser_output}/brotway-browser-result.json"
        )
        browser.log(f"Chromium Brotway probe result:\n{browser_result}")
        browser.succeed(
            f"${browserPython}/bin/python -c \"import json; "
            f"result=json.load(open('{browser_output}/brotway-browser-result.json')); "
            "assert 'error' not in result and result.get('input') == 'triple-Shift', result\""
        )

        # Triple-Shift is intercepted in broadway.js and causes broadwayd to
        # spawn this native GTK helper. Its process and private GTK mapping are
        # an unambiguous browser-input -> daemon -> GTK round trip.
        wait_for(
            deck,
            "triple-Shift debug-menu private GTK mapping",
            "for pid in $(pgrep -u ${toString deckUid} -f '[g]tk4-brotway-debugmenu'); do "
            "grep -q '/lib/gtk4-brotway/libgtk-4.so' /proc/$pid/maps && exit 0; "
            "done; exit 1",
            timeout=30,
        )
        deck.succeed(as_deck("systemctl --user is-active --quiet keymasq-brotway.service"))
        browser.succeed(f"test -s {browser_output}/brotway-before.png")
        browser.succeed(f"test -s {browser_output}/brotway-after-triple-shift.png")
        browser.succeed("systemctl stop keymasq-brotway-browser.service")

        # Launch the complete Keymasq icon inventory through the same installed
        # Brotway wrapper. The gallery resolves and decodes every icon file
        # before reporting success; Chromium separately proves the full gallery
        # surface traversed Brotway and captures it for visual inspection.
        deck.succeed(
            as_deck(
                f"rm -f {gallery_result_path}; "
                "systemd-run --user --unit=keymasq-icon-gallery --collect "
                f"--setenv=KEYMASQ_ICON_GALLERY_RESULT={gallery_result_path} "
                "--setenv=KEYMASQ_ICON_GALLERY_PORT=18102 -- "
                "${iconGallery}/run_icon_gallery.sh"
            )
        )
        wait_for(deck, "icon gallery result", f"test -s {gallery_result_path}", timeout=90)
        wait_for(deck, "icon gallery TCP listener", "ss -ltn | grep -q ':18102 '", timeout=90)
        wait_for(browser, "icon gallery HTTP page", "curl -fsS http://deck:18102/ >/dev/null", timeout=90)

        gallery_result = json.loads(deck.succeed(f"cat {gallery_result_path}"))
        deck.log(f"AppImage icon gallery result:\n{json.dumps(gallery_result, indent=2, sort_keys=True)}")
        assert gallery_result["tested_icons"] == expected_icon_names, gallery_result
        assert gallery_result["tested_assets"] == expected_assets, gallery_result
        assert gallery_result["total_icon_count"] == len(expected_icon_names), gallery_result
        assert gallery_result["available_icon_count"] == len(expected_icon_names), gallery_result
        assert gallery_result["missing_icons"] == [], gallery_result
        assert gallery_result["icon_errors"] == {}, gallery_result
        assert gallery_result["available_asset_count"] == len(expected_assets), gallery_result
        assert gallery_result["missing_assets"] == [], gallery_result
        assert gallery_result["asset_errors"] == {}, gallery_result
        resolved_icon_files = gallery_result["resolved_icon_files"]
        assert list(resolved_icon_files) == expected_icon_names, gallery_result
        assert all("/share/icons/Keymasq/" in path for path in resolved_icon_files.values()), gallery_result
        assert all(path.endswith(".png") for path in resolved_icon_files.values()), gallery_result

        browser.succeed(f"rm -rf {gallery_browser_output}; mkdir -p {gallery_browser_output}")
        browser.succeed(
            "systemd-run --unit=keymasq-icon-gallery-browser --collect "
            "--setenv=KEYMASQ_BROTWAY_URL=http://deck:18102/ "
            f"--setenv=KEYMASQ_BROTWAY_OUTPUT_DIR={gallery_browser_output} -- "
            "keymasq-appimage-icon-gallery-browser-probe"
        )
        wait_for(
            browser,
            "icon gallery Chromium result",
            f"test -s {gallery_browser_output}/icon-gallery-browser-result.json || "
            "! systemctl is-active --quiet keymasq-icon-gallery-browser.service",
            timeout=150,
        )
        gallery_browser_result = json.loads(
            browser.succeed(
                f"test -s {gallery_browser_output}/icon-gallery-browser-result.json; "
                f"cat {gallery_browser_output}/icon-gallery-browser-result.json"
            )
        )
        browser.log(
            "Chromium icon gallery probe result:\n"
            + json.dumps(gallery_browser_result, indent=2, sort_keys=True)
        )
        assert "error" not in gallery_browser_result, gallery_browser_result
        browser.succeed(f"test -s {gallery_browser_output}/icon-gallery.png")

        # A fresh process must produce the same complete audit. This catches
        # accidental dependence on state retained by the first GTK process.
        deck.succeed(as_deck("systemctl --user stop keymasq-icon-gallery.service"))
        wait_for(deck, "first icon gallery shutdown", "! ss -ltn | grep -q ':18102 '")
        deck.succeed(
            as_deck(
                f"rm -f {gallery_result_path}; "
                "systemd-run --user --unit=keymasq-icon-gallery-restart --collect "
                f"--setenv=KEYMASQ_ICON_GALLERY_RESULT={gallery_result_path} "
                "--setenv=KEYMASQ_ICON_GALLERY_PORT=18102 -- "
                "${iconGallery}/run_icon_gallery.sh"
            )
        )
        wait_for(deck, "restarted icon gallery result", f"test -s {gallery_result_path}", timeout=90)
        restarted_gallery_result = json.loads(deck.succeed(f"cat {gallery_result_path}"))
        assert restarted_gallery_result == gallery_result, (gallery_result, restarted_gallery_result)
        deck.succeed(
            as_deck(
                "! journalctl --user -u keymasq-icon-gallery.service "
                "-u keymasq-icon-gallery-restart.service --no-pager "
                "| grep -Eiq 'No image loaders are configured|Failed to load (image|icon)'"
            )
        )
        deck.succeed(as_deck("systemctl --user stop keymasq-icon-gallery-restart.service"))
    except Exception:
        dump_diagnostics("AppImage/Brotway integration failure")
        raise
  '';
}
