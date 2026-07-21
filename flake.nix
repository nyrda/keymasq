{
  description = "Keymasq - input remapper for keyboards, mice, and game controllers";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
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
              || rel == "docs/assets/screenshots"
              || pkgs.lib.hasPrefix "docs/assets/screenshots/" rel
              || pkgs.lib.hasPrefix ".venv/" rel
              || pkgs.lib.hasPrefix ".pytest_cache/" rel
              || pkgs.lib.hasPrefix ".ruff_cache/" rel
              || pkgs.lib.hasPrefix ".mypy_cache/" rel
              || base == "__pycache__"
              || pkgs.lib.hasSuffix ".pyc" base
            );
        };
      mkEvdevPackage =
        pkgs: pythonPackages:
        {
          version,
          hash,
          pyproject ? true,
        }:
        pythonPackages.buildPythonPackage {
          pname = "evdev";
          inherit version;
          format = if pyproject then "pyproject" else "setuptools";

          src = pkgs.fetchPypi {
            pname = "evdev";
            inherit version hash;
          };

          patchPhase = ''
            substituteInPlace setup.py \
              --replace-fail /usr/include ${pkgs.linuxHeaders}/include
          '';

          build-system = lib.optionals pyproject [ pythonPackages.setuptools ];
          nativeBuildInputs = lib.optionals (!pyproject) [ pythonPackages.setuptools ];
          buildInputs = [ pkgs.linuxHeaders ];

          doCheck = false;
          pythonImportsCheck = [ "evdev" ];

          meta = {
            description = "Provides bindings to the generic input event interface in Linux";
            homepage = "https://python-evdev.readthedocs.io/";
            changelog = "https://github.com/gvalkov/python-evdev/blob/v${version}/docs/changelog.rst";
            license = lib.licenses.bsd3;
            platforms = lib.platforms.linux;
          };
        };
      mkEvdevPackages =
        pkgs: pythonPackages:
        let
          mkEvdev = mkEvdevPackage pkgs pythonPackages;
        in
        {
          evdev161 = mkEvdev {
            version = "1.6.1";
            hash = "sha256-KZ24YozHOyN/wcxX08KUj6oHVuKli2GUtb+B3CCB8eM=";
            pyproject = false;
          };
          evdev170 = mkEvdev {
            version = "1.7.0";
            hash = "sha256-lb0qHgxs4s16LsxubNlzb/eUs61ctU2B2MvC5BTQuHA=";
          };
        };
      mkPackage =
        pkgs:
        { evdevPackage ? null }:
        let
          runtimePython = pkgs.python3;
          runtimePythonPackages = runtimePython.pkgs;
          resolvedEvdevPackage =
            if evdevPackage == null then
              runtimePythonPackages.evdev
            else
              evdevPackage;
        in
        runtimePythonPackages.buildPythonPackage {
          pname = "keymasq";
          version = "0.19.0";
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
            resolvedEvdevPackage
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
            install -Dm644 $src/udev/91-keymasq-acl.rules $out/lib/udev/rules.d/91-keymasq-acl.rules
            install -Dm644 $src/udev/99-keymasq-hide-grabbed.rules $out/lib/udev/rules.d/99-keymasq-hide-grabbed.rules
            substituteInPlace $out/lib/udev/rules.d/91-keymasq-acl.rules \
              --replace-fail /usr/bin/setfacl ${pkgs.acl}/bin/setfacl
            substituteInPlace $out/lib/udev/rules.d/99-keymasq-hide-grabbed.rules \
              --replace-fail /usr/bin/setfacl ${pkgs.acl}/bin/setfacl \
              --replace-fail /usr/bin/chmod ${pkgs.coreutils}/bin/chmod
            for icon in $src/assets/icons/tools.keymasq.keymasq-*.png; do
              size=''${icon##*-}
              size=''${size%.png}
              install -Dm644 "$icon" "$out/share/icons/hicolor/$size"x"$size"/apps/tools.keymasq.keymasq.png
            done
            install -Dm644 $src/polkit/com.keymasq.record-macro.policy $out/share/polkit-1/actions/com.keymasq.record-macro.policy
          '';

          meta = {
            description = "Input remapper for keyboards, mice, and game controllers, with layered profiles and macros";
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
      packagesFor = forAllSystems (
        system:
        let
          pkgs = mkPkgs system;
          evdevPackages = mkEvdevPackages pkgs pkgs.python3.pkgs;
        in
        {
          default = mkPackage pkgs { };
          keymasq-evdev161 = mkPackage pkgs {
            evdevPackage = evdevPackages.evdev161;
          };
          keymasq-evdev170 = mkPackage pkgs {
            evdevPackage = evdevPackages.evdev170;
          };
        }
      );
      listenerVmMatrix =
        let
          pkgs = mkPkgs "x86_64-linux";
          compositorVmLib = self.lib.compositorVmLib { inherit pkgs; };
        in
        import ./nix/listener-vm-matrix.nix {
          inherit pkgs compositorVmLib;
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
      pytestVmEvdev161Checks =
        let
          pkgs = mkPkgs "x86_64-linux";
          evdevPackages = mkEvdevPackages pkgs pkgs.python3.pkgs;
        in
        import ./nix/pytest-vm.nix {
          inherit pkgs;
          system = "x86_64-linux";
          keymasqPackage = packagesFor.x86_64-linux.keymasq-evdev161;
          keymasqModule = self.nixosModules.default;
          source = mkCleanSrc pkgs;
          checkName = "pytest-vm-evdev161";
          evdevPackage = evdevPackages.evdev161;
        };
      pytestVmEvdev170Checks =
        let
          pkgs = mkPkgs "x86_64-linux";
          evdevPackages = mkEvdevPackages pkgs pkgs.python3.pkgs;
        in
        import ./nix/pytest-vm.nix {
          inherit pkgs;
          system = "x86_64-linux";
          keymasqPackage = packagesFor.x86_64-linux.keymasq-evdev170;
          keymasqModule = self.nixosModules.default;
          source = mkCleanSrc pkgs;
          checkName = "pytest-vm-evdev170";
          evdevPackage = evdevPackages.evdev170;
        };
      daemonSessionIntegrationChecks =
        let
          pkgs = mkPkgs "x86_64-linux";
        in
        import ./nix/daemon-session-integration-test.nix {
          inherit pkgs;
          system = "x86_64-linux";
          keymasqPackage = packagesFor.x86_64-linux.default;
          keymasqModule = self.nixosModules.default;
        };
      daemonSessionIntegrationEvdev161Checks =
        let
          pkgs = mkPkgs "x86_64-linux";
          evdevPackages = mkEvdevPackages pkgs pkgs.python3.pkgs;
        in
        import ./nix/daemon-session-integration-test.nix {
          inherit pkgs;
          system = "x86_64-linux";
          keymasqPackage = packagesFor.x86_64-linux.keymasq-evdev161;
          keymasqModule = self.nixosModules.default;
          checkSuffix = "-evdev161";
          evdevPackage = evdevPackages.evdev161;
        };
      daemonSessionIntegrationEvdev170Checks =
        let
          pkgs = mkPkgs "x86_64-linux";
          evdevPackages = mkEvdevPackages pkgs pkgs.python3.pkgs;
        in
        import ./nix/daemon-session-integration-test.nix {
          inherit pkgs;
          system = "x86_64-linux";
          keymasqPackage = packagesFor.x86_64-linux.keymasq-evdev170;
          keymasqModule = self.nixosModules.default;
          checkSuffix = "-evdev170";
          evdevPackage = evdevPackages.evdev170;
        };
      appimageBrotwayArtifact = builtins.getEnv "KEYMASQ_APPIMAGE_TEST_ARTIFACT";
      appimageBrotwayIntegrationChecks =
        if appimageBrotwayArtifact == "" then
          { checks = { }; }
        else
          let
            pkgs = mkPkgs "x86_64-linux";
          in
          {
            checks.appimage-brotway-integration-test = import ./nix/appimage-brotway-integration-test.nix {
              inherit pkgs;
              appimageArtifact = builtins.path {
                path = appimageBrotwayArtifact;
                name = "Keymasq-under-test.AppImage";
              };
            };
          };
      docshotVm =
        let
          pkgs = mkPkgs "x86_64-linux";
        in
        import ./nix/docshot-vm.nix {
          inherit pkgs;
          system = "x86_64-linux";
          keymasqPackage = packagesFor.x86_64-linux.default;
          keymasqModule = self.nixosModules.default;
        };
    in
    {
      packages = lib.recursiveUpdate packagesFor {
        x86_64-linux = {
          listener-vm-tools = listenerVmMatrix.listenerVmTools;
          docshot-tools = docshotVm.docshotTools;
          docshots = docshotVm.docshots;
          docshots-light = docshotVm.docshotsLight;
          docshots-all = docshotVm.docshotsAll;
        };
      };

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/keymasq";
        };
      });

      lib.compositorVmLib = { pkgs }: import ./nix/compositor-vm-lib.nix { inherit pkgs; };

      checks = {
        x86_64-linux =
          listenerVmMatrix.checks
          // pytestVmChecks.checks
          // pytestVmEvdev161Checks.checks
          // pytestVmEvdev170Checks.checks
          // daemonSessionIntegrationChecks.checks
          // daemonSessionIntegrationEvdev161Checks.checks
          // daemonSessionIntegrationEvdev170Checks.checks
          // appimageBrotwayIntegrationChecks.checks
          // docshotVm.checks;
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

            services.udev.packages = [ cfg.package ];

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
                # CAP_DAC_OVERRIDE, the only granted capability: udevadm
                # trigger writes root-owned sysfs uevent files during source
                # hide/restore, and hidden nodes reset to root:root 0600 must
                # stay usable for grabs and force-feedback passthrough. Do not
                # add capabilities in any variant; see docs/SECURITY.md.
                AmbientCapabilities = [ "CAP_DAC_OVERRIDE" ];
                CapabilityBoundingSet = [ "CAP_DAC_OVERRIDE" ];
                ProtectSystem = "strict";
                ProtectHome = true;
                PrivateTmp = true;
                RuntimeDirectory = "keymasq";
                RuntimeDirectoryMode = "0755";
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
              enableDefaultPath = false;
              serviceConfig = {
                Type = "simple";
                Environment = [ "KEYMASQ_SESSION_RESTART_ON_DAEMON_DISCONNECT=1" ];
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
          devPythonPackages = devPython.pkgs;
          evdevPackages = mkEvdevPackages pkgs devPythonPackages;
          mkTestPython =
            evdevPackage:
            extraPackages:
            devPython.withPackages (
              ps: [
                ps.dbus-next
                evdevPackage
                ps.tomli-w
                ps.uvloop
                ps.xlib
                ps.pytest
                ps.pytest-asyncio
                ps.pytest-cov
                ps.pytest-xdist
              ] ++ extraPackages
            );
          mkCiShell =
            evdevPackage:
            pkgs.mkShell {
              packages = [
                (mkTestPython evdevPackage [ ])
              ];
            };
          mkCiGuiShell =
            evdevPackage:
            pkgs.mkShell {
              # Point gdk-pixbuf at the librsvg loaders cache so SVG icons
              # (Adwaita theme, GTK4 assets) render correctly inside the
              # dev shell — matches what wrapGAppsHook4 does for the
              # installed package.
              GDK_PIXBUF_MODULE_FILE = "${pkgs.librsvg}/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache";

              packages = [
                (mkTestPython evdevPackage [ pkgs.python312Packages.pygobject3 ])
                pkgs.gobject-introspection
                pkgs.gtk4
                pkgs.libadwaita
                pkgs.librsvg
                pkgs.adwaita-icon-theme
                pkgs.basedpyright
                pkgs.hicolor-icon-theme
                pkgs.stylelint
                pkgs.xorgserver
              ];
            };
        in
        {
          ci = mkCiShell devPythonPackages.evdev;

          ci-evdev161 = mkCiShell evdevPackages.evdev161;

          ci-evdev170 = mkCiShell evdevPackages.evdev170;

          ci-typecheck = pkgs.mkShell {
            packages = [
              (mkTestPython devPythonPackages.evdev [ pkgs.python312Packages.pygobject3 ])
              pkgs.basedpyright
            ];
          };

          ci-gui = mkCiGuiShell devPythonPackages.evdev;

          ci-gui-evdev161 = mkCiGuiShell evdevPackages.evdev161;

          ci-gui-evdev170 = mkCiGuiShell evdevPackages.evdev170;

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
              pkgs.stylelint
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
