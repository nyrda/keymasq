{
  description = "Keyforge - key remapping tool";

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
          pname = "keyforge";
          version = "0.3";
          pyproject = true;

          src = mkCleanSrc pkgs;

          postPatch = ''
            cat > keyforge/common/build_paths.py <<EOF
            KEYFORGE_RECORD_HELPER_PATH = "${placeholder "out"}/bin/keyforge-record"
            SLURP_PATH = "${pkgs.slurp}/bin/slurp"
            EOF

            substituteInPlace polkit/com.keyforge.record-macro.policy \
              --replace-fail "/usr/bin/keyforge-record" "${placeholder "out"}/bin/keyforge-record"
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
            xlib
            pygobject3
          ];

          postInstall = ''
            install -Dm644 $src/assets/keyforge.desktop $out/share/applications/keyforge.desktop
            install -Dm644 $src/assets/keyforge.metainfo.xml $out/share/metainfo/keyforge.metainfo.xml
            install -Dm644 $src/assets/keyforge.svg $out/share/icons/hicolor/scalable/apps/keyforge.svg
            for icon in $src/assets/icons/keyforge-*.png; do
              size=''${icon##*-}
              size=''${size%.png}
              install -Dm644 "$icon" "$out/share/icons/hicolor/$size"x"$size"/apps/keyforge.png
            done
            install -Dm644 $src/polkit/com.keyforge.record-macro.policy $out/share/polkit-1/actions/com.keyforge.record-macro.policy
          '';

          meta = {
            description = "A key remapping tool for Linux";
            homepage = "https://keyforge.tools";
            changelog = "https://github.com/nyrda/keyforge/blob/master/CHANGELOG.md";
            sourceProvenance = [ lib.sourceTypes.fromSource ];
            platforms = lib.platforms.linux;
            license = lib.licenses.mit;
            maintainers = [
              {
                name = "nyrda";
                email = "nyrda@keyforge.tools";
                github = "nyrda";
              }
            ];
            mainProgram = "keyforge";
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
          keyforgePackage = packagesFor.x86_64-linux.default;
          keyforgeModule = self.nixosModules.default;
        };
      pytestVmChecks =
        let
          pkgs = mkPkgs "x86_64-linux";
        in
        import ./nix/pytest-vm.nix {
          inherit pkgs;
          system = "x86_64-linux";
          keyforgePackage = packagesFor.x86_64-linux.default;
          keyforgeModule = self.nixosModules.default;
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
          program = "${self.packages.${system}.default}/bin/keyforge";
        };
      });

      checks = {
        x86_64-linux = listenerVmMatrix.checks // pytestVmChecks.checks;
      };

      nixosModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.services.keyforge;
          tomlFormat = pkgs.formats.toml { };
          defaultPackage =
            self.packages.${pkgs.stdenv.hostPlatform.system}.default
              or (throw "Keyforge does not provide a package for ${pkgs.stdenv.hostPlatform.system}");
        in
        {
          options.services.keyforge = {
            enable = lib.mkEnableOption "Keyforge key remapping daemon";

            package = lib.mkOption {
              type = lib.types.package;
              default = defaultPackage;
              defaultText = lib.literalExpression "self.packages.${pkgs.stdenv.hostPlatform.system}.default";
              description = "Keyforge package to use for the daemon, session service, and optional CLI/GUI install.";
            };

            installPackage = lib.mkEnableOption "install the Keyforge package into environment.systemPackages";

            securityConfig = lib.mkOption {
              type = lib.types.attrs;
              default = {
                daemon_allowed_uids = [ ];
                session_allowed_uids = [ ];
                macro.exec_timeout_max_ms = 30000;
                recording_guard = {
                  unlock_required = true;
                  macro_edit_requires_unlock = false;
                };
                session_command_acl.gui = [ ];
                session_command_acl.cli = [ ];
                daemon_command_acl.session = [ ];
              };
              description = "Security policy configuration (rendered to /etc/keyforge/security.toml)";
            };
          };

          config = lib.mkIf cfg.enable {
            users.users.keyforge = {
              isSystemUser = true;
              group = "keyforge";
              description = "Keyforge daemon user";
            };
            users.groups.keyforge = { };

            environment.etc."keyforge/security.toml".source =
              tomlFormat.generate "keyforge-security.toml" cfg.securityConfig;

            systemd.tmpfiles.rules = [
              "d /run/keyforge 0755 keyforge keyforge -"
              "d /var/lib/keyforge 0750 keyforge keyforge -"
            ];

            services.udev.extraRules = ''
              ACTION=="add|change", KERNEL=="uinput", GROUP="input", MODE="0660", RUN+="${pkgs.acl}/bin/setfacl -m u:keyforge:rw /dev/%k"
              ACTION=="add|change", SUBSYSTEM=="input", KERNEL=="event*", RUN+="${pkgs.acl}/bin/setfacl -m u:keyforge:rw /dev/input/%k"
            '';

            systemd.services.keyforged = {
              description = "Keyforge Input Remapping Daemon";
              wantedBy = [ "multi-user.target" ];
              after = [ "systemd-udev-settle.service" ];
              requires = [ "systemd-udev-settle.service" ];
              restartTriggers = [ cfg.package ];
              serviceConfig = {
                Type = "notify";
                User = "keyforge";
                Group = "keyforge";
                SupplementaryGroups = [ "input" ];
                ExecStartPre = [
                  "+${pkgs.acl}/bin/setfacl -m u:keyforge:rw /dev/uinput"
                  "+${pkgs.bash}/bin/sh -c 'for p in /dev/input/event*; do [ -e \"$p\" ] && ${pkgs.acl}/bin/setfacl -m u:keyforge:rw \"$p\"; done'"
                ];
                ExecStart = "${cfg.package}/bin/keyforged";
                Restart = "on-failure";
                RestartSec = 5;
                NoNewPrivileges = true;
                ProtectSystem = "strict";
                ProtectHome = true;
                PrivateTmp = true;
                StateDirectory = "keyforge";
                ReadWritePaths = [ "/run/keyforge" "/var/lib/keyforge" ];
              };
            };

            systemd.user.services.keyforge-session = {
              description = "Keyforge Session Manager";
              partOf = [ "graphical-session.target" ];
              wantedBy = [ "graphical-session.target" ];
              after = [ "graphical-session.target" ];
              restartTriggers = [ cfg.package ];
              serviceConfig = {
                Type = "simple";
                ExecStart = "${cfg.package}/bin/keyforge-session";
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
          devPythonPackages = pkgs.python312Packages;
        in
        {
          default = pkgs.mkShell {
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
                pygobject3
                xlib
                pytest
                pytest-asyncio
                pytest-cov
              ]))
              pkgs.gobject-introspection
              pkgs.gtk4
              pkgs.libadwaita
              pkgs.git
              pkgs.openssh
              pkgs.nfpm
              pkgs.gnupg
              pkgs.rpm
              devPythonPackages.mypy
              pkgs.ruff
              pkgs.basedpyright
              pkgs.dpkg
              pkgs.nodejs
              pkgs.cloc
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
