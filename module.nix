self:
{ config, lib, pkgs, ... }:

let
  cfg = config.programs.zelo;
  system = pkgs.stdenv.hostPlatform.system;
in
{
  options.programs.zelo = {
    enable = lib.mkEnableOption "zelo, the Zelotes F-36 CLI (package + udev rule)";

    gui.enable = lib.mkEnableOption "the Qt GUI (zelo-gui; it also provides the zelo command)";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${system}.default;
      defaultText = lib.literalExpression "zelo.packages.\${system}.default";
      description = "The CLI-only zelo package.";
    };

    guiPackage = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${system}.zelo-gui;
      defaultText = lib.literalExpression "zelo.packages.\${system}.zelo-gui";
      description = "The zelo package with the Qt GUI.";
    };
  };

  config = lib.mkIf cfg.enable {
    # one package only: both ship a `zelo` binary and the same python module
    environment.systemPackages = [ (if cfg.gui.enable then cfg.guiPackage else cfg.package) ];

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
