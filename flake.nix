{
  description = "Keymasq - key remapping tool";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      lib = nixpkgs.lib;
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = lib.genAttrs supportedSystems;
      mkPkgs = system: import nixpkgs { inherit system; };
      sourceRoot = ./.;
      mkCleanSrc = pkgs:
        pkgs.lib.cleanSourceWith {
          src = sourceRoot;
          filter =
            path: type:
            let
              pathStr = toString path;
              rootStr = toString sourceRoot;
              rel =
                if pathStr == rootStr then
                  ""
                else
                  pkgs.lib.removePrefix "${rootStr}/" pathStr;
              base = builtins.baseNameOf pathStr;
            in
            !(
              pkgs.lib.hasPrefix "build/" rel
              || pkgs.lib.hasPrefix "dist/" rel
              || pkgs.lib.hasPrefix "pkg/" rel
              || pkgs.lib.hasPrefix ".venv/" rel
              || pkgs.lib.hasPrefix ".pytest_cache/" rel
              || pkgs.lib.hasPrefix ".ruff_cache/" rel
              || pkgs.lib.hasPrefix ".mypy_cache/" rel
              || base == "__pycache__"
              || pkgs.lib.hasSuffix ".pyc" base
            );
        };
      mkPackage =
        pkgs:
        let
          runtimePython = pkgs.python3;
          runtimePythonPackages = runtimePython.pkgs;
        in
        runtimePythonPackages.buildPythonPackage {
          pname = "keymasq";
          version = "0.13.0";
          pyproject = true;

          src = mkCleanSrc pkgs;

          postPatch = ''
            cat > keymasq/common/build_paths.py <<EOF
            KEYMASQ_RECORD_HELPER_PATH = "${placeholder "out"}/bin/keymasq-record"
            SLURP_PATH = "${pkgs.slurp}/bin/slurp"
            EOF

            substituteInPlace polkit/com.keymasq.record-macro.policy \
              --replace-fail "/usr/bin/keymasq-record" "${placeholder "out"}/bin/keymasq-record"
          '';

          preFixup = ''
            gappsWrapperArgs+=(
              --prefix XDG_DATA_DIRS : "${pkgs.adwaita-icon-theme}/share:${pkgs.hicolor-icon-theme}/share"
              --prefix PATH : "/run/wrappers/bin"
            )
          '';

          nativeBuildInputs = [
            runtimePythonPackages.setuptools
            runtimePythonPackages.wheel
            pkgs.gobject-introspection
            pkgs.makeWrapper
            pkgs.wrapGAppsHook4
          ];

          buildInputs = [
            pkgs.gtk4
            pkgs.libadwaita
            pkgs.adwaita-icon-theme
            pkgs.hicolor-icon-theme
          ];

          propagatedBuildInputs = with runtimePythonPackages; [
            evdev
            tomli-w
            dbus-next
            uvloop
            xlib
            pygobject3
          ];

          postInstall = ''
            install -Dm644 $src/assets/tools.keymasq.keymasq.desktop $out/share/applications/tools.keymasq.keymasq.desktop
            install -Dm644 $src/assets/tools.keymasq.keymasq.metainfo.xml $out/share/metainfo/tools.keymasq.keymasq.metainfo.xml
            install -Dm644 $src/assets/tools.keymasq.keymasq.svg $out/share/icons/hicolor/scalable/apps/tools.keymasq.keymasq.svg
            for icon in $src/assets/icons/tools.keymasq.keymasq-*.png; do
              size=''${icon##*-}
              size=''${size%.png}
              install -Dm644 "$icon" "$out/share/icons/hicolor/$size"x"$size"/apps/tools.keymasq.keymasq.png
            done
            install -Dm644 $src/polkit/com.keymasq.record-macro.policy $out/share/polkit-1/actions/com.keymasq.record-macro.policy
          '';

          meta = {
            description = "A key remapping tool for Linux";
            homepage = "https://keymasq.tools";
            changelog = "https://github.com/nyrda/keymasq/blob/master/CHANGELOG.md";
            sourceProvenance = [ lib.sourceTypes.fromSource ];
            platforms = lib.platforms.linux;
            license = lib.licenses.mit;
            maintainers = [
              {
                name = "nyrda";
                email = "nyrda@keymasq.tools";
                github = "nyrda";
              }
            ];
            mainProgram = "keymasq";
          };
        };
      packagesFor = forAllSystems (system: {
        default = mkPackage (mkPkgs system);
      });
      listenerVmMatrix =
        let
          pkgs = mkPkgs "x86_64-linux";
        in
        import ./nix/listener-vm-matrix.nix {
          inherit pkgs;
          system = "x86_64-linux";
          keymasqPackage = packagesFor.x86_64-linux.default;
          keymasqModule = self.nixosModules.default;
        };
      pytestVmChecks =
        let
          pkgs = mkPkgs "x86_64-linux";
        in
        import ./nix/pytest-vm.nix {
          inherit pkgs;
          system = "x86_64-linux";
          keymasqPackage = packagesFor.x86_64-linux.default;
          keymasqModule = self.nixosModules.default;
          source = mkCleanSrc pkgs;
        };
    in
    {
      packages = lib.recursiveUpdate packagesFor {
        x86_64-linux.listener-vm-tools = listenerVmMatrix.listenerVmTools;
      };

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/keymasq";
        };
      });

      checks = {
        x86_64-linux = listenerVmMatrix.checks // pytestVmChecks.checks;
      };

      nixosModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.services.keymasq;
          tomlFormat = pkgs.formats.toml { };
          defaultPackage =
            self.packages.${pkgs.stdenv.hostPlatform.system}.default
              or (throw "Keymasq does not provide a package for ${pkgs.stdenv.hostPlatform.system}");
        in
        {
          options.services.keymasq = {
            enable = lib.mkEnableOption "Keymasq key remapping daemon";

            package = lib.mkOption {
              type = lib.types.package;
              default = defaultPackage;
              defaultText = lib.literalExpression "self.packages.${pkgs.stdenv.hostPlatform.system}.default";
              description = "Keymasq package to use for the daemon, session service, and optional CLI/GUI install.";
            };

            installPackage = lib.mkEnableOption "install the Keymasq package into environment.systemPackages";

            securityConfig = lib.mkOption {
              type = lib.types.attrs;
              default = {
                daemon_allowed_uids = [ ];
                session_allowed_uids = [ ];
                macro.exec_timeout_max_ms = 30000;
                gui = {
                  emergency_cancel_combo_enabled = true;
                };
                recording_guard = {
                  unlock_required = true;
                  macro_edit_requires_unlock = false;
                };
                session_command_acl.gui = [ ];
                session_command_acl.cli = [ ];
                daemon_command_acl.session = [ ];
              };
              description = "Security policy configuration (rendered to /etc/keymasq/security.toml)";
            };
          };

          config = lib.mkIf cfg.enable {
            users.users.keymasq = {
              isSystemUser = true;
              group = "keymasq";
              description = "Keymasq daemon user";
            };
            users.groups.keymasq = { };

            environment.etc."keymasq/security.toml".source =
              tomlFormat.generate "keymasq-security.toml" cfg.securityConfig;

            systemd.tmpfiles.rules = [
              "d /run/keymasq 0755 keymasq keymasq -"
              "d /var/lib/keymasq 0750 keymasq keymasq -"
            ];

            services.udev.extraRules = ''
              ACTION=="add|change", KERNEL=="uinput", GROUP="input", MODE="0660", RUN+="${pkgs.acl}/bin/setfacl -m u:keymasq:rw /dev/%k"
              ACTION=="add|change", SUBSYSTEM=="input", KERNEL=="event*", RUN+="${pkgs.acl}/bin/setfacl -m u:keymasq:rw /dev/input/%k"
            '';

            systemd.services.keymasqd = {
              description = "Keymasq Input Remapping Daemon";
              wantedBy = [ "multi-user.target" ];
              after = [
                "systemd-udevd.service"
                "systemd-udev-trigger.service"
              ];
              wants = [ "systemd-udev-trigger.service" ];
              restartTriggers = [ cfg.package ];
              serviceConfig = {
                Type = "notify";
                User = "keymasq";
                Group = "keymasq";
                SupplementaryGroups = [ "input" ];
                Nice = -5;
                ExecStartPre = [
                  "+${pkgs.acl}/bin/setfacl -m u:keymasq:rw /dev/uinput"
                  "+${pkgs.bash}/bin/sh -c 'for p in /dev/input/event*; do [ -e \"$p\" ] && ${pkgs.acl}/bin/setfacl -m u:keymasq:rw \"$p\"; done'"
                ];
                ExecStart = "${cfg.package}/bin/keymasqd";
                Restart = "on-failure";
                RestartSec = 5;
                NoNewPrivileges = true;
                ProtectSystem = "strict";
                ProtectHome = true;
                PrivateTmp = true;
                StateDirectory = "keymasq";
                ReadWritePaths = [ "/run/keymasq" "/var/lib/keymasq" ];
              };
            };

            systemd.user.services.keymasq-session = {
              description = "Keymasq Session Manager";
              partOf = [ "graphical-session.target" ];
              wantedBy = [ "graphical-session.target" ];
              after = [ "graphical-session.target" ];
              restartTriggers = [ cfg.package ];
              serviceConfig = {
                Type = "simple";
                ExecStart = "${cfg.package}/bin/keymasq-session";
                Restart = "on-failure";
                RestartSec = 3;
              };
            };

            environment.systemPackages = lib.mkIf cfg.installPackage [ cfg.package ];
          };
        };

      devShells = forAllSystems (
        system:
        let
          pkgs = mkPkgs system;
          devPython = pkgs.python312;
          mkTestPython =
            extraPackages:
            devPython.withPackages (
              ps: with ps; [
                dbus-next
                evdev
                tomli-w
                uvloop
                xlib
                pytest
                pytest-asyncio
                pytest-cov
              ] ++ extraPackages
            );
        in
        {
          ci = pkgs.mkShell {
            packages = [
              (mkTestPython [ ])
            ];
          };

          ci-gui = pkgs.mkShell {
            # Point gdk-pixbuf at the librsvg loaders cache so SVG icons
            # (Adwaita theme, GTK4 assets) render correctly inside the
            # dev shell — matches what wrapGAppsHook4 does for the
            # installed package.
            GDK_PIXBUF_MODULE_FILE = "${pkgs.librsvg}/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache";

            packages = [
              (mkTestPython [ pkgs.python312Packages.pygobject3 ])
              pkgs.gobject-introspection
              pkgs.gtk4
              pkgs.libadwaita
              pkgs.librsvg
              pkgs.adwaita-icon-theme
              pkgs.basedpyright
              pkgs.hicolor-icon-theme
              pkgs.xorgserver
            ];
          };

          default = pkgs.mkShell {
            # Point gdk-pixbuf at the librsvg loaders cache so SVG icons
            # (Adwaita theme, GTK4 assets) render correctly inside the
            # dev shell — matches what wrapGAppsHook4 does for the
            # installed package.
            GDK_PIXBUF_MODULE_FILE = "${pkgs.librsvg}/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache";

            packages = [
              (devPython.withPackages (ps: with ps; [
                build
                installer
                setuptools
                wheel
                pip
                dbus-next
                evdev
                tomli-w
                uvloop
                pygobject3
                xlib
                pytest
                pytest-asyncio
                pytest-cov
                # Docs site (mkdocs + Material theme). mkdocs-material
                # pulls in mkdocs, pymdown-extensions, and pygments as
                # transitive deps.
                mkdocs-material
              ]))
              pkgs.gobject-introspection
              pkgs.gtk4
              pkgs.libadwaita
              pkgs.librsvg
              pkgs.adwaita-icon-theme
              pkgs.hicolor-icon-theme
              pkgs.git
              pkgs.openssh
              pkgs.gnupg
              pkgs.rpm
              pkgs.python312Packages.mypy
              pkgs.ruff
              pkgs.basedpyright
              pkgs.dpkg
              pkgs.nodejs
              pkgs.cloc
              pkgs.glow
              pkgs.slurp
              pkgs.pv
              # GitHub Actions
              pkgs.gh           # GitHub CLI - manage workflows, PRs, releases
              pkgs.act          # run GitHub Actions locally
              pkgs.actionlint   # lint workflow YAML files
            ];
          };
        }
      );
    };
}
