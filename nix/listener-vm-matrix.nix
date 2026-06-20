{
  pkgs,
  system,
  keymasqPackage,
  keymasqModule,
  compositorVmLib,
}:

let
  lib = pkgs.lib;
  gnomeBridgeSource = ../gnome-extension + "/gnome-bridge@keymasq.tools";

  vmUser = "keymasqvm";
  vmUid = 1000;

  gnomeBridge = pkgs.runCommand "keymasq-gnome-bridge" { } ''
    mkdir -p "$out/share/gnome-shell/extensions/gnome-bridge@keymasq.tools"
    cp ${gnomeBridgeSource + "/extension.js"} \
      "$out/share/gnome-shell/extensions/gnome-bridge@keymasq.tools/extension.js"
    cp ${gnomeBridgeSource + "/metadata.json"} \
      "$out/share/gnome-shell/extensions/gnome-bridge@keymasq.tools/metadata.json"
  '';

  sessionQuery = pkgs.writeShellApplication {
    name = "keymasq-session-query";
    runtimeInputs = [ pkgs.python3 ];
    text = ''
      exec ${pkgs.python3}/bin/python ${./listener-vms/session-query.py} "$@"
    '';
  };

  gnomeBridgeProbe = pkgs.writeShellApplication {
    name = "keymasq-gnome-bridge-probe";
    runtimeInputs = [ pkgs.python3 ];
    text = ''
      exec ${pkgs.python3}/bin/python ${./listener-vms/gnome-bridge-probe.py} "$@"
    '';
  };

  listenerWindowLabTools = compositorVmLib.mkWindowLabTools {
    commandPrefix = "keymasq-listener";
    packageName = "keymasq-listener-vm-tools";
    extraPaths = [
      sessionQuery
      gnomeBridgeProbe
    ];
  };
  listenerVmTools = listenerWindowLabTools.tools;

  mkDesktopTest =
    {
      name,
      expectedCompositor,
      extraModule,
      runListenerAssertions ? true,
      beforeDesktopScript ? "",
      desktopReadyScript ? "",
      preflightScript ? "",
      memorySize ? 4096,
      activationMethod ? expectedCompositor,
    }:
    pkgs.testers.runNixOSTest {
      inherit name;

      nodes.machine = compositorVmLib.mkBaseDesktopModule {
        inherit
          name
          vmUser
          vmUid
          memorySize
          ;
        userDescription = "Listener VM test user";
        imports = [ keymasqModule ];
        extraGroups = [
          "wheel"
          "video"
          "input"
        ];
        systemPackages = [
          keymasqPackage
          listenerVmTools
          gnomeBridge
          pkgs.jq
          pkgs.wmctrl
          pkgs.xdotool
          pkgs.slurp
        ];
        extraConfig = lib.mkMerge [
          extraModule
          {
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
          }
        ];
      };

      testScript = ''
        ${compositorVmLib.testScriptPrelude {
          inherit vmUser vmUid;
          dumpFunctionName = "dump_session_debug";
          extraDebugScript = ''
            log_command_output("loginctl user-status", "loginctl user-status ${vmUser} || true")
            log_command_output(
                "journalctl display-manager",
                "journalctl -b -u display-manager.service --no-pager -n 200 || true",
            )
            log_command_output(
                "journalctl user@${toString vmUid}",
                "journalctl -b -u user@${toString vmUid}.service --no-pager -n 200 || true",
            )
            log_command_output(
                "user systemctl failed units",
                as_user("systemctl --user --failed --no-pager || true"),
            )
            log_command_output(
                "user default target",
                as_user("systemctl --user status default.target --no-pager || true"),
            )
            log_command_output(
                "keymasq-session user service",
                as_user("systemctl --user status keymasq-session.service --no-pager || true"),
            )
            log_command_output(
                "keymasq-session journal",
                as_user("journalctl --user -u keymasq-session.service --no-pager -n 200 || true"),
            )
            log_command_output(
                "gnome-bridge-probe user service",
                as_user("systemctl --user status gnome-bridge-probe.service --no-pager || true"),
            )
            log_command_output(
                "gnome-bridge-probe journal",
                as_user("journalctl --user -u gnome-bridge-probe.service --no-pager -n 200 || true"),
            )
          '';
        }}

        listener_socket = f"{runtime_dir}/listener-lab.sock"
        lab_prestarted = False

        def session_query(command: str) -> dict:
            raw = machine.succeed(
                as_user(
                    f"keymasq-session-query --socket {runtime_dir}/keymasq/session.sock --command {command}"
                )
            )
            return json.loads(raw)

        def session_query_json(command: str, extra_fields: dict) -> dict:
            """Send a session query with complex JSON fields via a payload file."""
            import base64
            payload_path = f"{runtime_dir}/session-query-payload.json"
            encoded = base64.b64encode(json.dumps(extra_fields).encode()).decode()
            machine.succeed(f"echo {encoded} | base64 -d > {payload_path}")
            machine.succeed(f"chown ${vmUser}: {payload_path}")
            raw = machine.succeed(
                as_user(
                    f"keymasq-session-query --socket {runtime_dir}/keymasq/session.sock "
                    f"--command {command} --payload-file {payload_path}"
                )
            )
            return json.loads(raw)

        def session_activate_title(title: str, timeout: int = 30) -> None:
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    raw = machine.succeed(
                        as_user(
                            f"keymasq-session-query --socket {runtime_dir}/keymasq/session.sock "
                            f"--command activate_title --field title={title}"
                        )
                    )
                    result = json.loads(raw)
                    machine.log(f"session_activate_title({title!r}): {result}")
                    if result.get("status") == "ok":
                        return
                except Exception as e:
                    machine.log(f"session_activate_title({title!r}) attempt failed: {e}")
                time.sleep(1)
            raise Exception(f"Failed to activate window {title!r} through keymasq-session")

        def gnome_activate_title(title: str, timeout: int = 30) -> None:
            """Ask the GNOME bridge extension to activate a window by title."""
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    raw = machine.succeed(
                        as_user(
                            f"keymasq-session-query --socket {runtime_dir}/keymasq/session.sock "
                            f"--command activate_title --field title={title}"
                        )
                    )
                    result = json.loads(raw)
                    machine.log(f"gnome_activate_title({title!r}): {result}")
                    if result.get("status") == "ok":
                        return
                except Exception as e:
                    machine.log(f"gnome_activate_title({title!r}) attempt failed: {e}")
                time.sleep(1)
            raise Exception(f"Failed to activate GNOME window {title!r}")

        def hyprland_activate_title(title: str) -> None:
            """Use hyprctl to focus a window by title on Hyprland."""
            sig = machine.succeed(as_user(
                "systemctl --user show-environment"
                " | sed -n 's/^HYPRLAND_INSTANCE_SIGNATURE=//p'"
            )).strip()
            machine.log(f"hyprland_activate_title({title!r}): sig={sig!r}")
            machine.succeed(
                f"runuser -u ${vmUser} -- env"
                f" HYPRLAND_INSTANCE_SIGNATURE={sig}"
                f" XDG_RUNTIME_DIR={runtime_dir}"
                f" hyprctl dispatch focuswindow title:{title}"
            )

        def sway_activate_title(title: str) -> None:
            """Use swaymsg to focus a window by title on Sway."""
            sock = machine.succeed(as_user(
                "systemctl --user show-environment"
                " | sed -n 's/^SWAYSOCK=//p'"
            )).strip()
            machine.log(f"sway_activate_title({title!r}): sock={sock!r}")
            machine.succeed(
                f"runuser -u ${vmUser} -- env"
                f" SWAYSOCK={sock}"
                f" XDG_RUNTIME_DIR={runtime_dir}"
                f' swaymsg "[title={title}] focus"'
            )

        def niri_activate_title(title: str, timeout: int = 30) -> None:
            """Use niri msg to focus a window by title on Niri."""
            escaped_title = title.replace("\\", "\\\\").replace('"', '\\"')
            sock = machine.succeed(as_user(
                "systemctl --user show-environment"
                " | sed -n 's/^NIRI_SOCKET=//p'"
            )).strip()
            machine.log(f"niri_activate_title({title!r}): sock={sock!r}")
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    window_id = machine.succeed(
                        f"runuser -u ${vmUser} -- env"
                        f" NIRI_SOCKET={sock}"
                        f" XDG_RUNTIME_DIR={runtime_dir}"
                        " sh -lc "
                        + shlex.quote(
                            "niri msg --json windows "
                            f'| jq -r --arg title "{escaped_title}" '
                            '".[] | select(.title == \\$title) | .id" '
                            "| head -n1"
                        )
                    ).strip()
                    if window_id:
                        machine.succeed(
                            f"runuser -u ${vmUser} -- env"
                            f" NIRI_SOCKET={sock}"
                            f" XDG_RUNTIME_DIR={runtime_dir}"
                            f" niri msg action focus-window --id {window_id}"
                        )
                        return
                except Exception as e:
                    machine.log(f"niri_activate_title({title!r}) attempt failed: {e}")
                time.sleep(1)
            raise Exception(f"Failed to activate Niri window {title!r}")

        def niri_focused_window() -> dict | None:
            sock = machine.succeed(as_user(
                "systemctl --user show-environment"
                " | sed -n 's/^NIRI_SOCKET=//p'"
            )).strip()
            raw = machine.succeed(
                f"runuser -u ${vmUser} -- env"
                f" NIRI_SOCKET={sock}"
                f" XDG_RUNTIME_DIR={runtime_dir}"
                " niri msg --json focused-window"
            ).strip()
            if not raw or raw == "null":
                return None
            return json.loads(raw)

        def niri_windows() -> list[dict]:
            sock = machine.succeed(as_user(
                "systemctl --user show-environment"
                " | sed -n 's/^NIRI_SOCKET=//p'"
            )).strip()
            raw = machine.succeed(
                f"runuser -u ${vmUser} -- env"
                f" NIRI_SOCKET={sock}"
                f" XDG_RUNTIME_DIR={runtime_dir}"
                " niri msg --json windows"
            ).strip()
            return json.loads(raw or "[]")

        def niri_window_by_title(title: str) -> dict | None:
            return next((window for window in niri_windows() if window.get("title") == title), None)

        def wait_for_listener() -> None:
            machine.log("Waiting for keymasq-session listener readiness")
            deadline = time.time() + 120
            last = None
            while time.time() < deadline:
                try:
                    payload = session_query("get_compositor")
                    last = payload
                    if (
                        payload.get("compositor_id") == "${expectedCompositor}"
                        and payload.get("listener_active") is True
                    ):
                        return
                except Exception as exc:
                    last = {"error": str(exc)}
                time.sleep(1)
            dump_session_debug("Timed out waiting for listener readiness")
            raise AssertionError(f"listener did not become ready: {last!r}")

        def wait_for_active_title(expected_title: str) -> dict:
            machine.log(f"Waiting for active window title: {expected_title}")
            deadline = time.time() + 90
            last = None
            last_logged = None
            while time.time() < deadline:
                payload = session_query("get_active_window")
                last = payload
                if payload != last_logged:
                    machine.log(f"Observed active window payload: {payload!r}")
                    last_logged = payload
                if payload.get("status") == "ok" and payload.get("title") == expected_title:
                    return payload
                time.sleep(1)
            dump_session_debug(f"Timed out waiting for active title {expected_title}")
            raise AssertionError(f"window title {expected_title!r} was not observed: {last!r}")

        def wait_for_any_active_window(timeout: int = 90) -> dict:
            machine.log("Waiting for any non-empty active window payload")
            deadline = time.time() + timeout
            last = None
            while time.time() < deadline:
                payload = session_query("get_active_window")
                last = payload
                if payload.get("status") == "ok" and (
                    payload.get("class") or payload.get("title") or payload.get("tags")
                ):
                    return payload
                time.sleep(1)
            dump_session_debug("Timed out waiting for any active window")
            raise AssertionError(f"active window was not observed: {last!r}")

        start_all()
        machine.wait_for_unit("display-manager.service")
        machine.wait_for_unit("graphical.target")
        wait_for_command(
            "logind session for ${vmUser}",
            "loginctl list-sessions --no-legend | grep -q '${vmUser}'",
        )
        wait_for_user_command("runtime directory", f"test -d {runtime_dir}")
        wait_for_user_command("user D-Bus socket", f"test -S {runtime_dir}/bus")
        wait_for_user_command("user default target", "systemctl --user is-active default.target")
        ${beforeDesktopScript}
        ${desktopReadyScript}
        ${preflightScript}
        if ${if runListenerAssertions then "True" else "False"}:
            machine.succeed(as_user("systemctl --user stop keymasq-session.service || true"))
            machine.succeed(as_user("systemctl --user start keymasq-session.service || true"))
            wait_for_user_command("keymasq-session user service", "systemctl --user is-active keymasq-session.service")
            wait_for_user_socket(
                "keymasq-session socket",
                f"{runtime_dir}/keymasq/session.sock",
                "keymasq-session.service",
            )

            with subtest("listener starts"):
                wait_for_listener()
                if "${expectedCompositor}" == "kde":
                    wait_for_user_command(
                        "KDE listener script loaded",
                        "journalctl --user -u keymasq-session.service --no-pager "
                        "| grep -F 'KDE listener script loaded'",
                    )

            with subtest("window detection and switching"):
                if "${expectedCompositor}" == "gnome":
                    wait_for_user_command(
                        "GNOME bridge connected",
                        "journalctl --user -u keymasq-session.service --no-pager "
                        "| grep -F 'GNOME bridge connected'",
                    )
                if not lab_prestarted:
                    machine.succeed(as_user(f"rm -f {listener_socket}"))
                    machine.succeed(
                        as_user(
                            "systemd-run --user --unit=keymasq-window-lab "
                            "--collect keymasq-listener-window-lab "
                            f"--socket {listener_socket} "
                            "--app-id tools.keymasq.ListenerLab"
                        )
                    )
                    wait_for_user_socket(
                        "window lab socket",
                        listener_socket,
                        "keymasq-window-lab.service",
                        probe_cmd=(
                            "keymasq-listener-window-labctl "
                            f"--socket {listener_socket} snapshot >/dev/null"
                        ),
                    )

                machine.succeed(
                    as_user(
                        f"keymasq-listener-window-labctl --socket {listener_socket} "
                        "open alpha Alpha"
                    )
                )
                if "${activationMethod}" == "gnome":
                    time.sleep(1)
                    gnome_activate_title("Alpha")
                    wait_for_any_active_window()
                elif "${activationMethod}" == "niri":
                    session_activate_title("Alpha")
                    machine.log(f"niri windows after Alpha activate: {niri_windows()}")
                    machine.log(
                        f"niri focused window after Alpha activate: {niri_focused_window()}"
                    )
                alpha = wait_for_active_title("Alpha")
                assert alpha.get("class"), alpha

                machine.succeed(
                    as_user(
                        f"keymasq-listener-window-labctl --socket {listener_socket} "
                        "open beta Beta"
                    )
                )
                if "${activationMethod}" == "gnome":
                    time.sleep(1)
                    gnome_activate_title("Beta")
                elif "${activationMethod}" == "niri":
                    session_activate_title("Beta")
                beta = wait_for_active_title("Beta")
                assert beta.get("class") == alpha.get("class"), (alpha, beta)

                if "${activationMethod}" == "gnome":
                    gnome_activate_title("Alpha")
                elif "${activationMethod}" == "hyprland":
                    hyprland_activate_title("Alpha")
                elif "${activationMethod}" == "niri":
                    session_activate_title("Alpha")
                elif "${activationMethod}" == "sway":
                    sway_activate_title("Alpha")
                else:
                    machine.succeed(
                        as_user(
                            f"keymasq-listener-window-labctl --socket {listener_socket} focus alpha"
                        )
                    )
                wait_for_active_title("Alpha")

                machine.succeed(
                    as_user(
                        f"keymasq-listener-window-labctl --socket {listener_socket} "
                        "retitle alpha AlphaRenamed"
                    )
                )
            wait_for_active_title("AlphaRenamed")

            machine.succeed(
                as_user(
                    f"keymasq-listener-window-labctl --socket {listener_socket} close alpha"
                )
            )
            wait_for_active_title("Beta")

            if "${expectedCompositor}" == "niri":
                before = niri_window_by_title("Beta")
                assert before is not None, before
                assert before.get("title") == "Beta", before
                machine.log(f"niri Beta window before dispatch: {before}")

                dispatch = session_query_json(
                    "dispatch_compositor",
                    {
                        "compositor": "niri",
                        "dispatcher": "toggle-window-floating",
                        "args": "",
                    },
                )
                machine.log(f"dispatch_compositor: {dispatch}")
                assert dispatch.get("status") == "ok", dispatch

                deadline = time.time() + 30
                after = None
                while time.time() < deadline:
                    after = niri_window_by_title("Beta")
                    if after is not None and after.get("title") == "Beta":
                        before_floating = bool(before.get("is_floating"))
                        after_floating = bool(after.get("is_floating"))
                        if before_floating != after_floating:
                            break
                    time.sleep(1)

                assert after is not None, after
                machine.log(f"niri Beta window after dispatch: {after}")
                assert after.get("title") == "Beta", after
                assert bool(before.get("is_floating")) != bool(after.get("is_floating")), (
                    before,
                    after,
                )

            machine.succeed(
                as_user(f"keymasq-listener-window-labctl --socket {listener_socket} quit")
            )

            with subtest("cursor position"):
                uses_slurp = "${expectedCompositor}" in ("wayland", "cosmic", "niri")
                explicit_cursor_dispatch = "${expectedCompositor}" in ("gnome", "hyprland")
                native_target_x = 160
                native_target_y = 120
                if uses_slurp:
                    # Slurp-based compositors need keymasqd uinput devices and a __slurp_trigger macro.
                    # Create a hardware config for the QEMU AT keyboard so keymasqd grabs it.
                    import base64
                    hw_toml = (
                        '[hardware]\n'
                        'vendor_id = "0001"\n'
                        'product_id = "0001"\n'
                        'name = "QEMU AT Keyboard"\n'
                        '\n'
                        '[[hardware.evdev.devices]]\n'
                        'path = "/dev/input/by-path/platform-i8042-serio-0-event-kbd"\n'
                        'type = "keyboard"\n'
                        'id = "kb"\n'
                        '\n'
                        '[[hardware.layout.buttons]]\n'
                        'id = "key_a"\n'
                        'label = "A"\n'
                        'evdev = "key_a"\n'
                    )
                    hw_dir = "/home/${vmUser}/.config/keymasq/hardware"
                    machine.succeed(as_user("mkdir -p " + hw_dir))
                    hw_b64 = base64.b64encode(hw_toml.encode()).decode()
                    machine.succeed(f"echo {hw_b64} | base64 -d > " + hw_dir + "/0001_0001.toml")
                    machine.succeed("chown ${vmUser}: " + hw_dir + "/0001_0001.toml")

                    # Create a profile that grabs all interfaces on this hardware.
                    profile_toml = (
                        '[profile]\n'
                        'name = "cursor-test"\n'
                        'enabled = true\n'
                        'is_permanent = true\n'
                        'created_at = "2026-01-01T00:00:00"\n'
                        '\n'
                        '[devices."0001:0001"]\n'
                        'always_grab_all = true\n'
                    )
                    prof_dir = "/home/${vmUser}/.config/keymasq/profiles"
                    machine.succeed(as_user("mkdir -p " + prof_dir))
                    prof_b64 = base64.b64encode(profile_toml.encode()).decode()
                    machine.succeed(f"echo {prof_b64} | base64 -d > " + prof_dir + "/cursor-test.toml")
                    machine.succeed("chown ${vmUser}: " + prof_dir + "/cursor-test.toml")

                    # Tell the session to reload configs and grab the device.
                    reeval = session_query("reevaluate_hardware")
                    machine.log(f"reevaluate_hardware: {reeval}")
                    assert reeval.get("status") == "ok", reeval

                    # Wait for the device grab to complete (uinput devices created).
                    time.sleep(2)

                    # Verify the device was grabbed by checking session journal.
                    wait_for_user_command(
                        "keymasqd grabbed device",
                        "journalctl --user -u keymasq-session.service --no-pager "
                        "| grep -F 'Grabbed device 0001:0001'",
                        timeout=30,
                    )

                    # keymasqd registers the internal __slurp_trigger macro at startup.
                    # Do not recreate it here; names starting with "__" are reserved.

                    # Give the compositor time to discover the new keymasq-mouse
                    # uinput device via libinput/udev before we query cursor position.
                    wait_for_command(
                        "keymasq-mouse uinput visible",
                        "ls /dev/input/by-id/ 2>/dev/null | grep -qF keymasq || "
                        "cat /proc/bus/input/devices | grep -qF keymasq-mouse",
                        timeout=15,
                    )
                    time.sleep(1)

                # Move cursor to a known position via QEMU tablet input.
                # Coordinates are in the usb-tablet range (0-32767) mapped to display pixels.
                machine.send_monitor_command("mouse_move 16384 12288 0")
                time.sleep(1)

                # Query cursor position.  Slurp-based compositors may need a
                # retry: the first attempt can fail when the compositor hasn't
                # fully registered the uinput mouse yet or when the layer
                # surface isn't ready before the macro click fires.
                cursor = None
                slurp_attempts = 3 if uses_slurp else 1
                for attempt in range(1, slurp_attempts + 1):
                    # Re-nudge the cursor before each attempt so the QEMU
                    # tablet position is fresh for the compositor.
                    if attempt > 1:
                        machine.log(f"Cursor position attempt {attempt}/{slurp_attempts}")
                        machine.send_monitor_command("mouse_move 16384 12288 0")
                        time.sleep(2)
                    cursor = session_query("get_cursor_position")
                    machine.log(f"get_cursor_position (attempt {attempt}): {cursor}")
                    if cursor.get("status") == "ok":
                        break

                assert cursor is not None and cursor.get("status") == "ok", (
                    f"cursor query failed after {slurp_attempts} attempts: {cursor}"
                )
                cx = cursor.get("x", 0)
                cy = cursor.get("y", 0)
                machine.log(f"Cursor at ({cx}, {cy})")
                assert isinstance(cx, int) and isinstance(cy, int), cursor
                # QEMU tablet-to-screen mapping varies across compositor/display setups.
                # Treat any non-negative, on-screen coordinate as a successful cursor read.
                assert cx >= 0, f"cursor x={cx} is negative"
                assert cy >= 0, f"cursor y={cy} is negative"
                assert cx < 4096, f"cursor x={cx} is implausibly large"
                assert cy < 4096, f"cursor y={cy} is implausibly large"

                if explicit_cursor_dispatch:
                    dispatch = session_query_json(
                        "dispatch_compositor",
                        {
                            "compositor": "${expectedCompositor}",
                            "dispatcher": "set_cursor_position",
                            "args": f"{native_target_x} {native_target_y}",
                        },
                    )
                    machine.log(f"dispatch set_cursor_position: {dispatch}")
                    assert dispatch.get("status") == "ok", dispatch

                    moved = None
                    deadline = time.time() + 10
                    while time.time() < deadline:
                        moved = session_query("get_cursor_position")
                        machine.log(f"get_cursor_position after compositor set: {moved}")
                        if (
                            moved.get("status") == "ok"
                            and moved.get("x") == native_target_x
                            and moved.get("y") == native_target_y
                        ):
                            break
                        time.sleep(1)

                    assert moved is not None and moved.get("status") == "ok", moved
                    assert moved.get("x") == native_target_x, moved
                    assert moved.get("y") == native_target_y, moved
      '';
    };

  gnomeModule = lib.mkMerge [
    (compositorVmLib.desktopModules.gnome {
      enableXserver = true;
      dconfExtensions = [ "gnome-bridge@keymasq.tools" ];
    })
    {
      systemd.user.services.keymasq-session = {
        wantedBy = lib.mkForce [ ];
        partOf = lib.mkForce [ ];
        after = lib.mkForce [ ];
      };

      services.gnome.gnome-initial-setup.enable = false;
    }
  ];

  kdeModule = compositorVmLib.desktopModules.kde { enableXserver = true; };
  hyprlandModule = compositorVmLib.desktopModules.hyprland { withUWSM = true; };
  xfceModule = compositorVmLib.desktopModules.xfce;
  cosmicModule = compositorVmLib.desktopModules.cosmic { };
  swayModule = compositorVmLib.desktopModules.sway;
  niriModule = compositorVmLib.desktopModules.niri { };

in
{
  inherit listenerVmTools;

  checks = {
    listener-vm-gnome-bridge = mkDesktopTest {
      name = "listener-vm-gnome-bridge";
      expectedCompositor = "gnome";
      extraModule = gnomeModule;
      runListenerAssertions = false;
      memorySize = 4096;
      beforeDesktopScript = ''
        bridge_output = f"{runtime_dir}/gnome-bridge-probe.json"
        bridge_debug = f"{runtime_dir}/gnome-bridge-probe.debug"
        machine.succeed(
            as_user(
                f"rm -f {listener_socket} {runtime_dir}/keymasq/gnome-bridge.sock {bridge_output} {bridge_debug}"
            )
        )
        machine.succeed(
            as_user(
                "systemd-run --user --unit=gnome-bridge-probe "
                "${gnomeBridgeProbe}/bin/keymasq-gnome-bridge-probe "
                f"--socket {runtime_dir}/keymasq/gnome-bridge.sock "
                f"--output {bridge_output} "
                f"--debug-output {bridge_debug} "
                "--timeout 180 "
                "--require-focus"
            )
        )
      '';
      desktopReadyScript = ''
        wait_for_user_command("GNOME Shell process", "pgrep -u 1000 gnome-shell")
        wait_for_user_command("GNOME Wayland socket", f"test -S {runtime_dir}/wayland-0")
        wait_for_user_command(
            "GNOME bridge extension visible",
            "gnome-extensions info gnome-bridge@keymasq.tools >/dev/null",
        )
        machine.succeed(as_user("gnome-extensions enable gnome-bridge@keymasq.tools"))
        wait_for_user_command(
            "GNOME bridge extension enabled",
            "gnome-extensions list --enabled | grep -Fx gnome-bridge@keymasq.tools",
        )
      '';
      preflightScript = ''
        log_command_output(
            "gnome-bridge-probe status after launch",
            as_user("systemctl --user status gnome-bridge-probe.service --no-pager || true"),
        )
        log_command_output(
            "keymasq runtime dir after probe launch",
            as_user(f"ls -la {runtime_dir}/keymasq || true"),
        )
        log_command_output(
            "gnome-bridge-probe debug after launch",
            as_user(f"cat {bridge_debug} || true"),
        )
        machine.succeed(as_user("gnome-extensions disable gnome-bridge@keymasq.tools || true"))
        machine.succeed(as_user("gnome-extensions enable gnome-bridge@keymasq.tools"))
        wait_for_user_command("GNOME bridge probe output", f"test -f {bridge_output}")
        log_command_output(
            "gnome-bridge-probe debug",
            as_user(f"cat {bridge_debug} || true"),
        )
        bridge_result = machine.succeed(
            as_user("systemctl --user show gnome-bridge-probe.service --property=ExecMainStatus --value")
        ).strip()
        assert bridge_result == "0", bridge_result
        bridge_payload = json.loads(machine.succeed(as_user(f"cat {bridge_output}")))
        assert bridge_payload.get("hello") is True, bridge_payload
        assert bridge_payload.get("pointer") is not None, bridge_payload
        assert bridge_payload.get("focus_titles"), bridge_payload
      '';
    };

    listener-vm-gnome = mkDesktopTest {
      name = "listener-vm-gnome";
      expectedCompositor = "gnome";
      extraModule = gnomeModule;
      memorySize = 4096;
      desktopReadyScript = ''
        wait_for_user_command("GNOME Shell process", "pgrep -u 1000 gnome-shell")
        wait_for_user_command("GNOME Wayland socket", f"test -S {runtime_dir}/wayland-0")
        wait_for_user_command(
            "WAYLAND_DISPLAY in systemd user env",
            "systemctl --user show-environment | grep -q WAYLAND_DISPLAY",
        )
        wait_for_user_command(
            "GNOME bridge extension visible",
            "gnome-extensions info gnome-bridge@keymasq.tools >/dev/null",
        )
        machine.succeed(as_user("gnome-extensions enable gnome-bridge@keymasq.tools"))
        wait_for_user_command(
            "GNOME bridge extension enabled",
            "gnome-extensions list --enabled | grep -Fx gnome-bridge@keymasq.tools",
        )
      '';
    };

    listener-vm-kde = mkDesktopTest {
      name = "listener-vm-kde";
      expectedCompositor = "kde";
      extraModule = kdeModule;
      memorySize = 4096;
      desktopReadyScript = ''
        wait_for_command("kwin_wayland process", "pgrep -u ${toString vmUid} kwin_wayland")
        wait_for_command("plasmashell process", "pgrep -u ${toString vmUid} plasmashell")
      '';
    };

    listener-vm-hyprland = mkDesktopTest {
      name = "listener-vm-hyprland";
      expectedCompositor = "hyprland";
      extraModule = hyprlandModule;
      memorySize = 3072;
      desktopReadyScript = ''
        wait_for_command("Hyprland process", "pgrep -u ${toString vmUid} Hyprland")
        wait_for_user_command(
            "Hyprland socket directory",
            "ls $XDG_RUNTIME_DIR/hypr/*/",
        )
        wait_for_user_command(
            "HYPRLAND_INSTANCE_SIGNATURE in systemd env",
            "systemctl --user show-environment | grep -q HYPRLAND_INSTANCE_SIGNATURE",
        )
      '';
    };

    listener-vm-xfce = mkDesktopTest {
      name = "listener-vm-xfce";
      expectedCompositor = "x11";
      extraModule = xfceModule;
      memorySize = 3072;
      desktopReadyScript = ''
        wait_for_command("xfwm4 process", "pgrep -u ${toString vmUid} xfwm4")
        wait_for_command("xfdesktop process", "pgrep -u ${toString vmUid} xfdesktop")
        wait_for_user_command("DISPLAY in environment", "test -n \"$DISPLAY\"")
        wait_for_user_command("X11 socket", "test -S /tmp/.X11-unix/X0")
      '';
    };

    listener-vm-cosmic = mkDesktopTest {
      name = "listener-vm-cosmic";
      expectedCompositor = "cosmic";
      extraModule = cosmicModule;
      memorySize = 4096;
      desktopReadyScript = ''
        wait_for_command("cosmic-comp process", "pgrep -u ${toString vmUid} cosmic-comp")
        wait_for_user_command(
            "Wayland socket",
            "ls $XDG_RUNTIME_DIR/wayland-*",
        )
        wait_for_user_command(
            "WAYLAND_DISPLAY in systemd user env",
            "systemctl --user show-environment | grep -q WAYLAND_DISPLAY",
        )
      '';
    };

    listener-vm-sway = mkDesktopTest {
      name = "listener-vm-sway";
      expectedCompositor = "wayland";
      activationMethod = "sway";
      extraModule = swayModule;
      memorySize = 3072;
      desktopReadyScript = ''
        wait_for_command("sway process", "pgrep -u ${toString vmUid} sway")
        wait_for_user_command(
            "Wayland socket",
            "ls $XDG_RUNTIME_DIR/wayland-*",
        )
        wait_for_user_command(
            "WAYLAND_DISPLAY in systemd user env",
            "systemctl --user show-environment | grep -q WAYLAND_DISPLAY",
        )
        wait_for_user_command(
            "SWAYSOCK in systemd user env",
            "systemctl --user show-environment | grep -q SWAYSOCK",
        )
      '';
    };

    listener-vm-niri = mkDesktopTest {
      name = "listener-vm-niri";
      expectedCompositor = "niri";
      activationMethod = "niri";
      extraModule = niriModule;
      memorySize = 3072;
      desktopReadyScript = ''
        wait_for_command("niri process", "pgrep -u ${toString vmUid} niri")
        wait_for_user_command(
            "Wayland socket",
            "ls $XDG_RUNTIME_DIR/wayland-*",
        )
        wait_for_user_command(
            "WAYLAND_DISPLAY in systemd user env",
            "systemctl --user show-environment | grep -q WAYLAND_DISPLAY",
        )
        wait_for_user_command(
            "NIRI_SOCKET in systemd user env",
            "systemctl --user show-environment | grep -q NIRI_SOCKET",
        )
        wait_for_user_command(
            "niri socket",
            'test -S "$(systemctl --user show-environment | sed -n "s/^NIRI_SOCKET=//p")"',
        )
      '';
    };

  };
}
