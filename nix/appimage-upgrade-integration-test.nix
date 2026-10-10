{
  pkgs,
  appimageArtifact,
}:

let
  deckUser = "deck";
  deckUid = 1000;
  runtimeDir = "/run/user/${toString deckUid}";
  baselineAppimage = pkgs.fetchurl {
    url = "https://github.com/nyrda/keymasq/releases/download/v0.20.0/Keymasq-0.20.0-x86_64.AppImage";
    hash = "sha256-ZT02SMkgp43B2EuMUF6nHKL7uYCVNCSuslFSRfG8sns=";
  };
  probePython = pkgs.python3.withPackages (ps: [ ps.evdev ]);
  upgradeProbe = pkgs.writeShellApplication {
    name = "keymasq-upgrade-probe";
    runtimeInputs = [
      probePython
      pkgs.systemd
    ];
    text = ''
      exec ${probePython}/bin/python ${./appimage-upgrade-integration-test/probe.py} "$@"
    '';
  };
in
pkgs.testers.runNixOSTest {
  name = "appimage-upgrade-integration-test";

  nodes.deck = {
    imports = [ (import ./appimage-deck-node.nix { inherit pkgs deckUser deckUid; }) ];
    virtualisation.diskSize = 4096;
    environment.systemPackages = [
      pkgs.gnupg
      upgradeProbe
    ];
  };

  testScript = ''
    import json
    import shlex
    import time

    baseline = "${baselineAppimage}"
    candidate = "${appimageArtifact}"
    hardware_fixture = "${./appimage-upgrade-integration-test/hardware.toml}"
    profile_fixture = "${./appimage-upgrade-integration-test/profile.toml}"
    home = "/home/${deckUser}"
    config_dir = f"{home}/.config/keymasq"
    update_dir = "/srv/keymasq-update"
    current_assets = "/opt/keymasq/runtime/current/share/keymasq/appimage"
    hardware_id = "cafe:0003"
    profile_name = "Upgrade Profile"
    macro_events = [
        {"device_type": "keyboard", "type": 1, "code": 22, "value": 1, "t_us": 0},
        {"device_type": "keyboard", "type": 1, "code": 22, "value": 0, "t_us": 10000},
    ]
    legacy_helper_paths = [
        "/opt/keymasq/bin/keymasq-record",
        f"{home}/.local/bin/keymasq-record",
        "/etc/polkit-1/rules.d/50-keymasq-record.rules",
        "/usr/share/polkit-1/actions/com.keymasq.record-macro.policy",
    ]
    removed_helper_paths = legacy_helper_paths + [
        "/etc/polkit-1/rules.d/50-keymasq-helper.rules",
        "/usr/share/polkit-1/actions/com.keymasq.helper.policy",
    ]

    def as_deck(command: str) -> str:
        environment = (
            f"HOME={home} "
            "XDG_RUNTIME_DIR=${runtimeDir} "
            "DBUS_SESSION_BUS_ADDRESS=unix:path=${runtimeDir}/bus "
        )
        return "runuser -u ${deckUser} -- env " + environment + "sh -c " + shlex.quote(command)

    def log_command(label: str, command: str) -> None:
        status, output = deck.execute(command)
        deck.log(f"{label} (exit={status})\n{output}")

    def dump_diagnostics(label: str) -> None:
        deck.log(f"==== {label}: deck diagnostics ====")
        log_command("installed tree", "find /opt/keymasq -maxdepth 3 -printf '%M %u:%g %p -> %l\\n' 2>/dev/null | sort || true")
        log_command("polkit files", "ls -la /etc/polkit-1/rules.d /usr/share/polkit-1/actions 2>&1 || true")
        log_command("user wrappers", f"ls -la {home}/.local/bin 2>&1 || true")
        log_command("security policy", "cat /etc/keymasq/security.toml 2>&1 || true")
        log_command("keymasqd status", "systemctl status keymasqd.service --no-pager || true")
        log_command("keymasqd journal", "journalctl -b -u keymasqd.service --no-pager -n 300 || true")
        log_command("hardware job journal", "journalctl -b -u 'keymasq-hardware@*.service' --no-pager -n 200 || true")
        log_command("session journal", "journalctl -b _SYSTEMD_USER_UNIT=keymasq-session.service --no-pager -n 300 || true")
        log_command("source journal", "journalctl -b -u keymasq-upgrade-source.service --no-pager -n 100 || true")
        log_command("active profiles", as_deck("keymasq-upgrade-probe session '{\"command\": \"get_active_profiles\"}' || true"))

    def wait_for(label: str, command: str, timeout: int = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if deck.execute(command)[0] == 0:
                return
            time.sleep(0.5)
        raise Exception(f"timed out waiting for {label}: {command}")

    def session(payload: dict) -> dict:
        return json.loads(deck.succeed(as_deck("keymasq-upgrade-probe session " + shlex.quote(json.dumps(payload)))))

    def cli_json(arguments: str) -> dict:
        return json.loads(deck.succeed(as_deck(f"/opt/keymasq/bin/keymasq --json {arguments}")))

    def runtime_of(pid: str) -> set[str]:
        maps = deck.succeed(f"cat /proc/{pid}/maps")
        return {
            part.split("/")[0]
            for part in maps.split("/opt/keymasq/runtime/")[1:]
        }

    def service_pids() -> tuple[str, str]:
        daemon_pid = deck.succeed("systemctl show -P MainPID keymasqd.service").strip()
        session_pid = deck.succeed(as_deck("systemctl --user show -P MainPID keymasq-session.service")).strip()
        assert daemon_pid != "0" and session_pid != "0", (daemon_pid, session_pid)
        return daemon_pid, session_pid

    def wait_for_services() -> None:
        deck.wait_for_unit("keymasqd.service")
        wait_for("user session service", as_deck("systemctl --user is-active --quiet keymasq-session.service"))
        wait_for("session socket", "test -S ${runtimeDir}/keymasq/session.sock")
        wait_for(
            "session connected to keymasqd",
            as_deck("/opt/keymasq/bin/keymasq --json status | grep -q '\"keymasqd_connected\": true'"),
        )

    def source_event_name() -> str:
        return deck.succeed("basename $(cat /run/keymasq-upgrade-source.path)").strip()

    def assert_user_state() -> None:
        deck.succeed(as_deck(f"keymasq-upgrade-probe wait-mapping {hardware_id} {shlex.quote(profile_name)}"))
        profiles = {profile["name"]: profile for profile in cli_json("profiles list")["profiles"]}
        assert profiles[profile_name]["enabled"] is True, profiles
        assert profiles[profile_name]["active"] is True, profiles
        macros = {macro["name"]: macro for macro in cli_json("macros list")["macros"]}
        assert macros["upgrade-macro"]["event_count"] == 2, macros
        settings = session({"command": "get_settings"})
        assert settings["virtual_gamepad_count"] == 2, settings
        assert settings["keyboard_layout"] == "de", settings
        deck.succeed("keymasq-upgrade-probe expect-tap BTN_EAST KEY_Y")
        deck.succeed("keymasq-upgrade-probe expect-tap BTN_SOUTH KEY_U")
        event_name = source_event_name()
        wait_for("hidden flag for the grabbed source", f"test -e /run/keymasq/hidden/{event_name}")
        wait_for(
            "udev hide rule on the grabbed source",
            f"udevadm info --query=property --name=/dev/input/{event_name} | grep -qx LIBINPUT_IGNORE_DEVICE=1",
        )

    start_all()
    deck.wait_for_unit("multi-user.target")

    try:
        deck.succeed("modprobe fuse; modprobe uinput")
        deck.succeed("loginctl enable-linger ${deckUser}")
        deck.wait_for_unit("user@${toString deckUid}.service")
        wait_for("Deck user D-Bus", "test -S ${runtimeDir}/bus")

        with subtest("install the released v0.20.0 AppImage"):
            deck.succeed(f"install -m 0755 {baseline} /tmp/Keymasq-0.20.0.AppImage")
            baseline_hash = deck.succeed("sha256sum /tmp/Keymasq-0.20.0.AppImage | cut -d' ' -f1").strip()
            status, output = deck.execute("/tmp/Keymasq-0.20.0.AppImage --install --user ${deckUser} 2>&1", timeout=300)
            deck.log(f"v0.20.0 installer (exit={status})\n{output}")
            assert status == 0, output
            assert deck.succeed("readlink /opt/keymasq/runtime/current").strip() == baseline_hash
            wait_for_services()
            for path in legacy_helper_paths:
                deck.succeed(f"test -e {path}")
            deck.succeed("grep -q polkit.addRule /etc/polkit-1/rules.d/50-keymasq-record.rules")
            deck.succeed("grep -q 'keymasq-record hardware-operation' /etc/systemd/system/keymasq-hardware@.service")

        with subtest("create user state with v0.20.0"):
            deck.succeed("systemd-run --unit=keymasq-upgrade-source --collect -- keymasq-upgrade-probe source")
            wait_for(
                "upgrade source gamepad",
                "test -S /run/keymasq-upgrade-source.sock && test -s /run/keymasq-upgrade-source.path",
            )
            macro_json = json.dumps({"events": macro_events})
            deck.succeed(as_deck(f"/opt/keymasq/bin/keymasq macros create upgrade-macro {shlex.quote(macro_json)}"))
            deck.succeed(
                as_deck(
                    f"install -D -m 0644 {hardware_fixture} {config_dir}/hardware/cafe_0003.toml && "
                    f"install -D -m 0644 {profile_fixture} {config_dir}/profiles/upgrade-profile.toml"
                )
            )
            session({"command": "reload"})
            session({"command": "reevaluate_hardware"})
            deck.succeed(as_deck(f"/opt/keymasq/bin/keymasq profiles enable {shlex.quote(profile_name)}"))
            settings = session({"command": "set_settings", "virtual_gamepad_count": 2, "keyboard_layout": "de"})
            assert settings.get("persisted", True) is True, settings
            assert_user_state()

        with subtest("write the legacy recording_guard keys"):
            deck.succeed(
                "sed -i -e 's/^unlock_required = .*/unlock_required = true/' "
                "-e 's/^macro_edit_requires_unlock = .*/macro_edit_requires_unlock = true/' "
                "/etc/keymasq/security.toml"
            )
            deck.succeed("grep -qx 'unlock_required = true' /etc/keymasq/security.toml")
            deck.succeed("grep -qx 'macro_edit_requires_unlock = true' /etc/keymasq/security.toml")
            policy_hash = deck.succeed("sha256sum /etc/keymasq/security.toml").strip()

        with subtest("publish the candidate as a signed update"):
            deck.succeed(f"install -d -m 0755 {update_dir}")
            deck.succeed(f"install -m 0755 {candidate} {update_dir}/Keymasq-candidate.AppImage")
            candidate_hash = deck.succeed(f"sha256sum {update_dir}/Keymasq-candidate.AppImage | cut -d' ' -f1").strip()
            candidate_version = deck.succeed(f"{update_dir}/Keymasq-candidate.AppImage --version").split()[-1]
            manifest = {
                "version": candidate_version,
                "architecture": "x86_64",
                "sha256": candidate_hash,
                "appimage_url": f"file://{update_dir}/Keymasq-candidate.AppImage",
            }
            deck.succeed(f"printf '%s' {shlex.quote(json.dumps(manifest))} > {update_dir}/latest-x86_64.json")
            deck.succeed(
                "export GNUPGHOME=$(mktemp -d); "
                "gpg --batch --passphrase ''' --quick-gen-key 'Keymasq upgrade test <upgrade-test@keymasq.invalid>' ed25519 sign never; "
                f"gpg --batch --armor --export > {update_dir}/update-key.asc; "
                f"gpg --batch --detach-sign --output {update_dir}/latest-x86_64.json.sig {update_dir}/latest-x86_64.json"
            )

        with subtest("self-update from v0.20.0 to the candidate"):
            old_daemon_pid, old_session_pid = service_pids()
            assert runtime_of(old_daemon_pid) == {baseline_hash}
            cursor = deck.succeed("journalctl -n 0 --show-cursor --no-pager | sed -n 's/^-- cursor: //p'").strip()
            assert cursor, "no journal cursor"
            status, output = deck.execute(
                f"KEYMASQ_APPIMAGE_UPDATE_BASE_URL=file://{update_dir} "
                f"KEYMASQ_APPIMAGE_UPDATE_PUBLIC_KEY={update_dir}/update-key.asc "
                "/opt/keymasq/bin/keymasq --self-update --user ${deckUser} 2>&1",
                timeout=300,
            )
            deck.log(f"self-update (exit={status})\n{output}")
            assert status == 0, output
            assert f"Keymasq updated to {candidate_version}" in output, output

        with subtest("the candidate runtime is installed and running"):
            assert deck.succeed("sha256sum /opt/keymasq/Keymasq.AppImage | cut -d' ' -f1").strip() == candidate_hash
            assert deck.succeed("readlink /opt/keymasq/runtime/current").strip() == candidate_hash
            assert deck.succeed("cat /opt/keymasq/version").strip() == candidate_version
            wait_for_services()
            daemon_pid, session_pid = service_pids()
            assert daemon_pid != old_daemon_pid, (old_daemon_pid, daemon_pid)
            assert session_pid != old_session_pid, (old_session_pid, session_pid)
            assert runtime_of(daemon_pid) == {candidate_hash}, runtime_of(daemon_pid)
            assert runtime_of(session_pid) == {candidate_hash}, runtime_of(session_pid)
            assert deck.succeed("systemctl show -P NRestarts keymasqd.service").strip() == "0"

        with subtest("integration files come from the candidate"):
            for asset, installed in (
                ("keymasqd.service", "/etc/systemd/system/keymasqd.service"),
                ("keymasq-hardware@.service", "/etc/systemd/system/keymasq-hardware@.service"),
                ("49-keymasq-hardware.rules", "/etc/polkit-1/rules.d/49-keymasq-hardware.rules"),
                ("91-keymasq-acl.rules", "/etc/udev/rules.d/91-keymasq-acl.rules"),
                ("99-keymasq-hide-grabbed.rules", "/etc/udev/rules.d/99-keymasq-hide-grabbed.rules"),
                ("keymasq-session.service", f"{home}/.config/systemd/user/keymasq-session.service"),
            ):
                deck.succeed(f"cmp {current_assets}/{asset} {installed}")
            deck.succeed("grep -q 'keymasq-helper hardware-operation' /etc/systemd/system/keymasq-hardware@.service")

        with subtest("the keymasq-record helper is migrated to keymasq-helper"):
            for wrapper in ("/opt/keymasq/bin/keymasq-helper", f"{home}/.local/bin/keymasq-helper"):
                deck.succeed(f"test -x {wrapper}")
                deck.succeed(f"grep -qF '\"$APPDIR/bin/keymasq-helper\"' {wrapper}")
                deck.succeed(f"test -x /opt/keymasq/runtime/{candidate_hash}/bin/keymasq-helper")
                deck.succeed(as_deck(f"{wrapper} --help"))
            for path in removed_helper_paths:
                deck.succeed(f"test ! -e {path}")
            keymasq_rules = deck.succeed("ls /etc/polkit-1/rules.d | grep keymasq").split()
            assert keymasq_rules == ["49-keymasq-hardware.rules"], keymasq_rules
            deck.succeed("! ls /usr/share/polkit-1/actions | grep -i keymasq")
            deck.succeed(f"test -f {current_assets}/50-keymasq-record.rules")
            deck.succeed(f"! grep -q addRule {current_assets}/50-keymasq-record.rules")

        with subtest("legacy recording_guard keys only produce warnings"):
            assert deck.succeed("sha256sum /etc/keymasq/security.toml").strip() == policy_hash
            for key in ("unlock_required", "macro_edit_requires_unlock"):
                warning = f"Ignoring recording_guard.{key} in /etc/keymasq/security.toml"
                deck.succeed(
                    f"journalctl --after-cursor={shlex.quote(cursor)} -u keymasqd.service --no-pager "
                    f"| grep -qF {shlex.quote(warning)}"
                )
                deck.succeed(
                    f"journalctl --after-cursor={shlex.quote(cursor)} _SYSTEMD_USER_UNIT=keymasq-session.service "
                    f"--no-pager | grep -qF {shlex.quote(warning)}"
                )
            macro_json = json.dumps({"events": macro_events})
            deck.succeed(as_deck(f"/opt/keymasq/bin/keymasq macros create upgrade-macro-after {shlex.quote(macro_json)}"))
            deck.succeed(as_deck("/opt/keymasq/bin/keymasq macros delete upgrade-macro-after"))

        with subtest("profiles, macros and settings survive the update"):
            assert_user_state()

        with subtest("the candidate keymasq-hardware@ jobs succeed"):
            event_name = source_event_name()
            deck.succeed(as_deck(f"/opt/keymasq/bin/keymasq profiles disable {shlex.quote(profile_name)}"))
            released = session({"command": "release_device", "hardware_id": hardware_id, "immediate": True})
            assert released["status"] == "ok", released
            wait_for("hidden flag cleared", f"test ! -e /run/keymasq/hidden/{event_name}")
            wait_for(
                "udev hide rule cleared",
                f"! udevadm info --query=property --name=/dev/input/{event_name} | grep -qx LIBINPUT_IGNORE_DEVICE=1",
            )
            hide_cursor = deck.succeed("journalctl -n 0 --show-cursor --no-pager | sed -n 's/^-- cursor: //p'").strip()
            deck.succeed(as_deck(f"/opt/keymasq/bin/keymasq profiles enable {shlex.quote(profile_name)}"))
            assert_user_state()
            jobs = deck.succeed(
                f"journalctl --after-cursor={shlex.quote(hide_cursor)} -u 'keymasq-hardware@*.service' --no-pager -o cat"
            )
            deck.log(f"hardware jobs after the update:\n{jobs}")
            assert "Finished Keymasq privileged hardware operation." in jobs, jobs
            assert ".service: Deactivated successfully." in jobs, jobs
            assert "Failed" not in jobs and "FAILURE" not in jobs, jobs
    except Exception:
        dump_diagnostics("AppImage upgrade failure")
        raise
  '';
}
