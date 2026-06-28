{ pkgs, system, keymasqPackage, keymasqModule }:

let
  lib = pkgs.lib;
  vmUser = "keymasqdoc";
  vmUid = 1000;
  runtimeDir = "/run/user/${toString vmUid}";
  deviceMap = "/run/keymasq-docshots/devices.json";
  sessionEnvFile = "/home/${vmUser}/.docshot-session-env";
  outputDir = "/home/${vmUser}/.local/share/keymasq-docshots/screenshots";

  giTypelibPath = lib.makeSearchPath "lib/girepository-1.0" [
    pkgs.cairo
    pkgs.gdk-pixbuf
    pkgs.glib
    pkgs.graphene
    pkgs.gtk4
    pkgs.libadwaita
    pkgs.pango
  ];

  docshotPython = pkgs.python3.withPackages (
    ps: with ps; [
      dbus-next
      evdev
      pygobject3
      tomli-w
      uvloop
      xlib
    ]
  );

  keymasqPythonPath = "${keymasqPackage}/${pkgs.python3.sitePackages}";
  docshotPath = lib.makeBinPath [
    keymasqPackage
    pkgs.imagemagick
    pkgs.usbutils
    pkgs.xdotool
  ];

  mkDocshotScript =
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
        docshotPython
        pkgs.adwaita-icon-theme
        pkgs.cairo
        pkgs.gdk-pixbuf
        pkgs.glib
        pkgs.graphene
        pkgs.gtk4
        pkgs.hicolor-icon-theme
        pkgs.libadwaita
        pkgs.librsvg
        pkgs.pango
      ];

      preFixup = ''
        gappsWrapperArgs+=(
          --prefix GI_TYPELIB_PATH : "${giTypelibPath}"
          --prefix PYTHONPATH : "${keymasqPythonPath}"
          --prefix PATH : "${docshotPath}"
          --set GDK_PIXBUF_MODULE_FILE "${pkgs.librsvg}/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
        )
      '';

      installPhase = ''
        mkdir -p "$out/bin"
        install -m755 ${script} "$out/bin/${name}"
        sed -i '1s|.*|#!${docshotPython}/bin/python|' "$out/bin/${name}"
      '';
    };

  docshotDevices = mkDocshotScript "keymasq-docshot-devices" ./docshots/devices.py;
  docshotSeed = mkDocshotScript "keymasq-docshot-seed" ./docshots/seed.py;
  docshotGuiDriver = mkDocshotScript "keymasq-docshot-gui-driver" ./docshots/gui_driver.py;

  docshotTools = pkgs.symlinkJoin {
    name = "keymasq-docshot-tools";
    paths = [
      docshotDevices
      docshotSeed
      docshotGuiDriver
    ];
  };

  userCommand =
    cmdEscaped:
    "runuser -u ${vmUser} -- sh -lc ${cmdEscaped}";

  mkDocshotTest =
    {
      name,
      modes,
    }:
    let
      modeList = lib.filter (mode: mode != "") (lib.splitString "," modes);
      modeWaits = lib.concatMapStringsSep "\n" (
        mode:
        "wait_for_user_command(\"docshot ${mode} screenshots\", \"test -d ${outputDir}/${mode}\")"
      ) modeList;
    in
    pkgs.testers.runNixOSTest {
      inherit name;

      nodes.machine =
        { ... }:
        {
          imports = [ keymasqModule ];

          documentation.nixos.enable = false;

          virtualisation = {
            graphics = true;
            memorySize = 4096;
            cores = 2;
            resolution = {
              x = 1280;
              y = 1400;
            };
            qemu.options = [
              "-vga none"
              "-device virtio-gpu-pci,xres=1280,yres=1400"
            ];
          };

          networking.hostName = "docshot-vm";
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

          systemd.services.keymasq-docshot-devices = {
            description = "Keymasq documentation screenshot virtual input devices";
            wantedBy = [ "multi-user.target" ];
            before = [ "keymasqd.service" ];
            serviceConfig = {
              Type = "simple";
              ExecStart = "${docshotTools}/bin/keymasq-docshot-devices --output ${deviceMap}";
              Restart = "on-failure";
              RestartSec = 1;
            };
          };

          systemd.services.keymasqd = {
            requires = [ "keymasq-docshot-devices.service" ];
            after = [ "keymasq-docshot-devices.service" ];
          };

          users.users.${vmUser} = {
            isNormalUser = true;
            uid = vmUid;
            description = "Documentation screenshot user";
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
            docshotTools
            pkgs.adwaita-icon-theme
            pkgs.hicolor-icon-theme
            pkgs.imagemagick
            pkgs.jq
            pkgs.usbutils
            pkgs.xdotool
          ];
        };

      testScript = ''
import shlex
import time

def as_user(cmd: str) -> str:
    script = (
        "export HOME=/home/${vmUser}; "
        "export XDG_RUNTIME_DIR=${runtimeDir}; "
        "export DBUS_SESSION_BUS_ADDRESS=unix:path=${runtimeDir}/bus; "
        "if [ -f ${sessionEnvFile} ]; then . ${sessionEnvFile}; fi; "
        "export DISPLAY=${"$"}{DISPLAY:-:0}; "
        "export GDK_BACKEND=x11; "
        f"{cmd}"
    )
    return "${userCommand "{cmdEscaped}"}".replace("{cmdEscaped}", shlex.quote(script))

def log_command_output(label: str, cmd: str) -> None:
    status, output = machine.execute(cmd)
    machine.log(f"{label} (exit={status})\n{output}")

def dump_debug(label: str) -> None:
    machine.log(f"==== {label} ====")
    log_command_output("keymasqd status", "systemctl status keymasqd.service --no-pager || true")
    log_command_output("docshot devices status", "systemctl status keymasq-docshot-devices.service --no-pager || true")
    log_command_output("docshot devices journal", "journalctl -b -u keymasq-docshot-devices.service --no-pager -n 160 || true")
    log_command_output("keymasqd journal", "journalctl -b -u keymasqd.service --no-pager -n 240 || true")
    log_command_output("keymasq-session status", as_user("systemctl --user status keymasq-session.service --no-pager || true"))
    log_command_output("keymasq-session journal", as_user("journalctl --user -u keymasq-session.service --no-pager -n 240 || true"))
    log_command_output("docshot generated files", as_user("find ~/.config/keymasq -maxdepth 3 -type f -print | sort || true"))
    log_command_output("docshot output files", as_user("find ${outputDir} -type f -print | sort || true"))
    log_command_output("input devices", "cat /proc/bus/input/devices || true")

def wait_for_command(label: str, command: str, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc = machine.execute(command)[0]
        if rc == 0:
            return
        time.sleep(1)
    dump_debug(f"timed out waiting for {label}")
    raise Exception(f"Timed out waiting for {label}: {command}")

def wait_for_user_command(label: str, command: str, timeout: int = 120) -> None:
    wait_for_command(label, as_user(command), timeout=timeout)

def must_run(command: str, timeout: int | None = None) -> str:
    if timeout is None:
        rc, output = machine.execute(command)
    else:
        rc, output = machine.execute(command, timeout=timeout)
    if rc != 0:
        dump_debug(f"command failed: {command}")
        raise Exception(f"Command failed with exit code {rc}: {command}\n{output}")
    return output

start_all()
machine.wait_for_unit("display-manager.service")
machine.wait_for_unit("graphical.target")
machine.wait_for_unit("keymasq-docshot-devices.service")
wait_for_command("docshot device map", "test -s ${deviceMap}")
wait_for_user_command("xfce session", "pgrep -u ${toString vmUid} xfce4-session")
wait_for_user_command("runtime directory", "test -d ${runtimeDir}")
wait_for_user_command("user bus", "test -S ${runtimeDir}/bus")
wait_for_user_command("X11 socket", "test -S /tmp/.X11-unix/X0")

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
    "XDG_SESSION_TYPE": session_env.get("XDG_SESSION_TYPE", "x11"),
    "XDG_CURRENT_DESKTOP": session_env.get("XDG_CURRENT_DESKTOP", "XFCE"),
}
env_file_body = "\n".join(
    f"export {key}={shlex.quote(value)}"
    for key, value in session_exports.items()
    if value
)
must_run("cat > ${sessionEnvFile} <<'EOF'\n" + env_file_body + "\nEOF")
must_run("chown ${vmUser}:users ${sessionEnvFile}")
must_run("chmod 600 ${sessionEnvFile}")

must_run(
    as_user(
        "mkdir -p ~/.config/gtk-4.0 ~/.config/gtk-3.0 && "
        "printf \"[Settings]\\ngtk-enable-animations=false\\ngtk-cursor-blink=false\\ngtk-font-name=Cantarell 11\\n\" > ~/.config/gtk-4.0/settings.ini && "
        "printf \"[Settings]\\ngtk-enable-animations=false\\ngtk-cursor-blink=false\\ngtk-font-name=Cantarell 11\\n\" > ~/.config/gtk-3.0/settings.ini"
    )
)

must_run(
    "keymasq-docshot-seed "
    "--config-home /home/${vmUser}/.config "
    "--state-dir /var/lib/keymasq "
    "--devices-json ${deviceMap} "
    "--config-owner ${vmUser} "
    "--state-owner keymasq"
)

must_run("systemctl restart keymasqd.service")
machine.wait_for_unit("keymasqd.service")
must_run(as_user("systemctl --user restart keymasq-session.service || systemctl --user start keymasq-session.service"))
wait_for_user_command("keymasq-session service", "systemctl --user is-active keymasq-session.service")
wait_for_user_command("keymasq-session socket", "test -S ${runtimeDir}/keymasq/session.sock")
wait_for_user_command(
    "keymasq-session status",
    "keymasq status --json | jq -e '.status == \"ok\"' >/dev/null",
)

must_run(as_user("rm -rf ${outputDir} && mkdir -p ${outputDir}"))
must_run(
    as_user(
        "keymasq-docshot-gui-driver "
        "--manifest ${../docs/screenshots.toml} "
        "--output-root ${outputDir} "
        "--modes ${modes}"
    ),
    timeout=1200,
)
${modeWaits}
machine.copy_from_machine("${outputDir}")
      '';
    };

  docshotTest = mkDocshotTest {
    name = "docshot-vm";
    modes = "dark";
  };

  docshotLightTest = mkDocshotTest {
    name = "docshot-vm-light";
    modes = "light";
  };

  docshotAllTest = mkDocshotTest {
    name = "docshot-vm-all";
    modes = "dark,light";
  };
in
{
  inherit docshotTools;

  docshots = docshotTest;
  docshotsLight = docshotLightTest;
  docshotsAll = docshotAllTest;

  checks = {
    docshot-vm = docshotTest;
  };
}
