self:
{ config, lib, pkgs, ... }:

let
  cfg = config.programs.zelo;
in
{
  options.programs.zelo = {
    enable = lib.mkEnableOption "zelo, the Zelotes F-36 RGB panel CLI (package + udev rule)";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = lib.literalExpression "zelo.packages.\${system}.default";
      description = "The zelo package to install.";
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ cfg.package ];

    # Let the logged-in (seat) user talk to the mouse's hidraw nodes without root.
    # Must be a rules.d file numbered < 73: `uaccess` is processed by
    # 73-seat-late.rules, so services.udev.extraRules (99-local.rules) is too late.
    services.udev.packages = [
      (pkgs.writeTextDir "lib/udev/rules.d/70-zelo.rules" ''
        SUBSYSTEM=="hidraw", ATTRS{idVendor}=="25a7", ATTRS{idProduct}=="fa68", TAG+="uaccess"
      '')
    ];
  };
}
