{ pkgs, keymasqPackage, keymasqModule }:

let
  testPython = pkgs.python3.withPackages (ps: [ ps.evdev ]);
  control = ./masking-recovery-test/control.py;
  pythonPath = "${keymasqPackage}/${pkgs.python3.sitePackages}:${./daemon-session-integration-test}:${./masking-recovery-test}";
in
{
  inherit testPython control pythonPath;

  machine = {
    imports = [ keymasqModule ];
    documentation.nixos.enable = false;
    virtualisation = {
      graphics = false;
      memorySize = 2048;
      cores = 2;
    };
    boot.kernelModules = [ "uhid" "uinput" ];
    services.keymasq = {
      enable = true;
      package = keymasqPackage;
      securityConfig = {
        daemon_allowed_uids = [ 1000 ];
        session_allowed_uids = [ 1000 ];
      };
    };
    users.users.masktest = {
      isNormalUser = true;
      uid = 1000;
      extraGroups = [ "input" ];
    };
    # Model desktop access that the production masking rules must override.
    services.udev.extraRules = ''
      SUBSYSTEM=="hidraw", KERNELS=="0005:2DC8:6012.*", GROUP="input", MODE="0660", TAG+="uaccess"
    '';
    systemd.services.keymasq-test-devices = {
      wantedBy = [ "multi-user.target" ];
      before = [ "keymasqd.service" ];
      environment.PYTHONPATH = pythonPath;
      serviceConfig.ExecStart = "${testPython}/bin/python ${control} serve";
    };
    environment.systemPackages = [ pkgs.acl testPython ];
  };
}
