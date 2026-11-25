{
  description = "zelo - Zelotes F-36 mouse control (CLI + Qt GUI)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: rec {
        zelo = pkgs.callPackage ./package.nix { };
        zelo-gui = pkgs.callPackage ./gui.nix { };
        default = zelo;
      });

      apps = forAllSystems (pkgs:
        let p = self.packages.${pkgs.stdenv.hostPlatform.system}; in
        rec {
          zelo = { type = "app"; program = nixpkgs.lib.getExe p.zelo; };
          gui = { type = "app"; program = nixpkgs.lib.getExe p.zelo-gui; };
          default = zelo;
        });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ (pkgs.python3.withPackages (ps: [ ps.pyside6 ])) ];
        };
      });

      overlays.default = final: _prev: {
        zelo = final.callPackage ./package.nix { };
        zelo-gui = final.callPackage ./gui.nix { };
      };

      nixosModules.default = import ./module.nix self;
      nixosModules.zelo = self.nixosModules.default;
    };
}
