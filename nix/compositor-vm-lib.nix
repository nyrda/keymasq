{ pkgs }:

let
  lib = pkgs.lib;

  indent =
    prefix: text:
    lib.concatMapStringsSep "\n" (line: prefix + line) (
      lib.splitString "\n" (lib.removeSuffix "\n" text)
    );

  giTypelibPath = lib.makeSearchPath "lib/girepository-1.0" [
    pkgs.cairo
    pkgs.gdk-pixbuf
    pkgs.glib
    pkgs.graphene
    pkgs.gtk4
    pkgs.pango
  ];

  gtkPython = pkgs.python3.withPackages (ps: [ ps.pygobject3 ]);

  defaultNiriConfig = ''
    input {
        focus-follows-mouse
        warp-mouse-to-focus mode="center-xy-always"
    }

    debug {
        honor-xdg-activation-with-invalid-serial
    }
  '';
in
rec {
  mkGtkScript =
    name: script:
    pkgs.stdenvNoCC.mkDerivation {
      pname = name;
      version = "1";
      dontUnpack = true;

      nativeBuildInputs = [
        pkgs.gobject-introspection
        pkgs.wrapGAppsHook4
      ];
      buildInputs = [
        gtkPython
        pkgs.adwaita-icon-theme
        pkgs.cairo
        pkgs.gdk-pixbuf
        pkgs.glib
        pkgs.graphene
        pkgs.gtk4
        pkgs.hicolor-icon-theme
        pkgs.pango
      ];

      preFixup = ''
        gappsWrapperArgs+=(
          --prefix GI_TYPELIB_PATH : "${giTypelibPath}"
        )
      '';

      installPhase = ''
        mkdir -p "$out/bin"
        install -m755 ${script} "$out/bin/${name}"
        sed -i '1s|.*|#!${gtkPython}/bin/python|' "$out/bin/${name}"
      '';
    };

  mkWindowLabTools =
    {
      commandPrefix,
      packageName,
      includeWaylandLauncher ? false,
      extraPaths ? [ ],
    }:
    let
      windowLabName = "${commandPrefix}-window-lab";
      windowLabCtlName = "${commandPrefix}-window-labctl";
      windowLab = mkGtkScript windowLabName ./compositor-vm-tools/window-lab.py;
      windowLabCtl = pkgs.writeShellApplication {
        name = windowLabCtlName;
        runtimeInputs = [ pkgs.python3 ];
        text = ''
          exec ${pkgs.python3}/bin/python ${./compositor-vm-tools/window-labctl.py} "$@"
        '';
      };
      windowLabWayland = pkgs.writeShellApplication {
        name = "${windowLabName}-wayland";
        runtimeInputs = [ windowLab ];
        text = ''
          export GDK_BACKEND=wayland
          export GSK_RENDERER=cairo
          export LIBGL_ALWAYS_SOFTWARE=1
          exec ${windowLabName} "$@"
        '';
      };
      paths =
        [
          windowLab
          windowLabCtl
        ]
        ++ lib.optionals includeWaylandLauncher [ windowLabWayland ]
        ++ extraPaths;
    in
    {
      inherit
        paths
        windowLab
        windowLabCtl
        windowLabName
        windowLabCtlName
        ;
      tools = pkgs.symlinkJoin {
        name = packageName;
        inherit paths;
      };
    }
    // lib.optionalAttrs includeWaylandLauncher {
      inherit windowLabWayland;
      windowLabWaylandName = "${windowLabName}-wayland";
    };

  mkBaseDesktopModule =
    {
      name,
      vmUser,
      vmUid,
      userDescription,
      memorySize ? 3072,
      cores ? 4,
      extraGroups ? [
        "video"
        "input"
      ],
      imports ? [ ],
      systemPackages ? [ ],
      extraConfig ? { },
    }:
    { ... }:
    {
      inherit imports;
      config = lib.mkMerge [
        {
          documentation.nixos.enable = false;

          virtualisation = {
            graphics = true;
            memorySize = memorySize;
            cores = cores;
          };

          networking.hostName = name;
          time.timeZone = "UTC";
          i18n.defaultLocale = "en_US.UTF-8";

          users.users.${vmUser} = {
            isNormalUser = true;
            uid = vmUid;
            description = userDescription;
            createHome = true;
            home = "/home/${vmUser}";
            inherit extraGroups;
          };

          services.displayManager.autoLogin = {
            enable = true;
            user = vmUser;
          };

          hardware.graphics.enable = true;
          services.dbus.enable = true;
          security.polkit.enable = true;
          services.libinput.enable = true;
          programs.dconf.enable = true;

          environment.systemPackages = systemPackages;
        }
        extraConfig
      ];
    };

  desktopModules = rec {
    sway = {
      services.displayManager.defaultSession = "sway";
      services.displayManager.sddm = {
        enable = true;
        wayland.enable = true;
      };
      programs.sway.enable = true;
    };

    xfce = {
      services.xserver.enable = true;
      services.displayManager.defaultSession = "xfce";
      services.xserver.displayManager.lightdm.enable = true;
      services.xserver.desktopManager.xfce.enable = true;
    };

    gnome =
      {
        enableXserver ? false,
        sessionPath ? [ ],
        extraGSettingsOverrides ? "",
        dconfExtensions ? [ ],
      }:
      lib.mkMerge [
        (lib.optionalAttrs enableXserver {
          services.xserver.enable = true;
        })
        {
          services.displayManager.defaultSession = "gnome";
          services.displayManager.gdm.enable = true;
          services.desktopManager.gnome =
            {
              enable = true;
            }
            // lib.optionalAttrs (sessionPath != [ ]) {
              inherit sessionPath;
            }
            // lib.optionalAttrs (extraGSettingsOverrides != "") {
              inherit extraGSettingsOverrides;
            };
        }
        (lib.optionalAttrs (dconfExtensions != [ ]) {
          programs.dconf.profiles.user.databases = [
            {
              settings = {
                "org/gnome/shell" = {
                  disable-user-extensions = false;
                  enabled-extensions = dconfExtensions;
                };
              };
            }
          ];
        })
      ];

    kde =
      { enableXserver ? false }:
      lib.mkMerge [
        (lib.optionalAttrs enableXserver {
          services.xserver.enable = true;
        })
        {
          services.displayManager.defaultSession = "plasma";
          services.displayManager.sddm = {
            enable = true;
            wayland.enable = true;
          };
          services.desktopManager.plasma6.enable = true;
        }
      ];

    hyprland =
      { withUWSM ? false }:
      {
        services.displayManager.defaultSession = if withUWSM then "hyprland-uwsm" else "hyprland";
        services.displayManager.sddm = {
          enable = true;
          wayland.enable = true;
        };
        programs.hyprland =
          {
            enable = true;
          }
          // lib.optionalAttrs withUWSM {
            withUWSM = true;
          };
      };

    cosmic =
      { sessionPackage ? cosmicMinimalSessionPackage }:
      {
        services.xserver.enable = true;
        services.displayManager.defaultSession = "cosmic-minimal";
        services.displayManager.sessionPackages = [ sessionPackage ];
        services.displayManager.sddm = {
          enable = true;
          wayland.enable = true;
        };
        environment.systemPackages = [ pkgs.cosmic-comp ];
      };

    niri =
      {
        package ? niriPatched,
        configText ? defaultNiriConfig,
      }:
      {
        services.displayManager.defaultSession = "niri";
        services.displayManager.sddm = {
          enable = true;
          wayland.enable = true;
        };
        programs.niri = {
          enable = true;
          package = package;
        };
        environment.etc."niri/config.kdl".text = configText;
      };
  };

  cosmicMinimalSessionPackage = pkgs.stdenvNoCC.mkDerivation {
    pname = "keymasq-cosmic-minimal-session";
    version = "1";
    dontUnpack = true;
    passthru.providedSessions = [ "cosmic-minimal" ];
    installPhase = ''
      mkdir -p "$out/bin" "$out/share/wayland-sessions"
      cat > "$out/bin/cosmic-minimal-session" <<'EOF'
#!${pkgs.bash}/bin/bash
set -euo pipefail
export XDG_CURRENT_DESKTOP=COSMIC
export DESKTOP_SESSION=cosmic
export XDG_SESSION_TYPE=wayland
exec ${pkgs.cosmic-comp}/bin/cosmic-comp
EOF
      chmod +x "$out/bin/cosmic-minimal-session"
      cat > "$out/share/wayland-sessions/cosmic-minimal.desktop" <<EOF
[Desktop Entry]
Name=COSMIC Minimal
Comment=Minimal COSMIC compositor session for Keymasq compositor VM tests
Exec=$out/bin/cosmic-minimal-session
Type=Application
DesktopNames=COSMIC
EOF
    '';
  };

  niriExpectedVersion = "26.04";

  niriPatched = assert pkgs.niri.version == niriExpectedVersion; pkgs.niri.overrideAttrs (old: {
    postPatch = (old.postPatch or "") + ''
      # Allow software EGL renderers (llvmpipe) for VM testing.
      sed -i 's/!egl_device\.is_software()/true/' src/backend/tty.rs

      if grep -q 'egl_device\.is_software()' src/backend/tty.rs; then
        echo "ERROR: niri software-renderer patch did not apply - see niriPatched in compositor-vm-lib.nix"
        exit 1
      fi
    '';
  });

  testScriptPrelude =
    {
      vmUser,
      vmUid,
      dumpFunctionName ? "dump_debug",
      extraDebugScript ? "",
    }:
    ''
      import base64
      import json
      import shlex
      import time

      runtime_dir = "/run/user/${toString vmUid}"

      def as_user(cmd: str) -> str:
          return (
              "runuser -u ${vmUser} -- env "
              f"HOME=/home/${vmUser} "
              f"XDG_RUNTIME_DIR={runtime_dir} "
              f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime_dir}/bus "
              "sh -lc "
              + shlex.quote(cmd)
          )

      def user_env(name: str) -> str:
          return machine.succeed(
              as_user(f"systemctl --user show-environment | sed -n 's/^{name}=//p'")
          ).strip()

      def as_user_with_env(env: dict[str, str], cmd: str) -> str:
          env_args = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
          return (
              "runuser -u ${vmUser} -- env "
              f"HOME=/home/${vmUser} "
              f"XDG_RUNTIME_DIR={runtime_dir} "
              f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime_dir}/bus "
              f"{env_args} sh -lc "
              + shlex.quote(cmd)
          )

      def process_env_value(process_pattern: str, variable: str) -> str:
          script = (
              f"pid=$(pgrep -u ${toString vmUid} -f {shlex.quote(process_pattern)} | head -n1); "
              "test -n \"$pid\"; "
              f"tr '\\0' '\\n' < /proc/$pid/environ | sed -n 's/^{variable}=//p' | head -n1"
          )
          return machine.succeed(script).strip()

      def set_user_environment(env: dict[str, str]) -> None:
          env_args = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
          machine.succeed(as_user(f"systemctl --user set-environment {env_args}"))

      def log_command_output(label: str, cmd: str) -> None:
          status, output = machine.execute(cmd, timeout=30)
          machine.log(f"{label} (exit={status})\n{output}")

      def ${dumpFunctionName}(label: str) -> None:
          machine.log(f"==== {label} ====")
          log_command_output("loginctl sessions", "loginctl list-sessions --no-legend || true")
          log_command_output(
              "display manager",
              "systemctl status display-manager.service --no-pager || true",
          )
          log_command_output(
              "user manager",
              "systemctl status user@${toString vmUid}.service --no-pager || true",
          )
          log_command_output("runtime dir", f"ls -la {runtime_dir} || true")
          log_command_output("user environment", as_user("systemctl --user show-environment || true"))
      ${indent "    " extraDebugScript}

      def wait_for_command(description: str, cmd: str, timeout: int = 180) -> str:
          machine.log(f"Waiting for {description}: {cmd}")
          deadline = time.time() + timeout
          last_status = None
          last_output = ""
          while time.time() < deadline:
              status, output = machine.execute(cmd, timeout=20)
              last_status = status
              last_output = output
              if status == 0:
                  return output
              time.sleep(1)
          ${dumpFunctionName}(f"Timed out waiting for {description}")
          raise AssertionError(
              f"{description} did not become ready (exit={last_status}): {last_output}"
          )

      def wait_for_user_command(description: str, cmd: str, timeout: int = 180) -> str:
          return wait_for_command(description, as_user(cmd), timeout=timeout)

      def wait_for_user_socket(
          description: str,
          socket_path: str,
          unit_name: str,
          timeout: int = 60,
          probe_cmd: str | None = None,
      ) -> None:
          deadline = time.time() + timeout
          socket_seen = False
          last_probe_status = None
          last_probe_output = ""
          while time.time() < deadline:
              status, _ = machine.execute(as_user(f"test -S {socket_path}"), timeout=20)
              if status == 0:
                  socket_seen = True
                  if probe_cmd is None:
                      return
                  ready_status, ready_output = machine.execute(as_user(probe_cmd), timeout=20)
                  last_probe_status = ready_status
                  last_probe_output = ready_output
                  if ready_status == 0:
                      return

              unit_status, _ = machine.execute(
                  as_user(f"systemctl --user is-failed {unit_name}"), timeout=20
              )
              if unit_status == 0:
                  ${dumpFunctionName}(f"{unit_name} failed while waiting for {description}")
                  raise AssertionError(
                      machine.succeed(
                          as_user(f"journalctl --user -u {unit_name} --no-pager -n 80 || true")
                      )
                  )
              time.sleep(1)

          ${dumpFunctionName}(f"Timed out waiting for {description}")
          if socket_seen:
              raise AssertionError(
                  f"{description} socket was created by {unit_name}, "
                  f"but readiness probe did not succeed "
                  f"(exit={last_probe_status}): {last_probe_output}"
              )
          raise AssertionError(f"{description} was not created by {unit_name}")
    '';
}
