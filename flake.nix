{
  description = "zelo - Zelotes F-36 RGB panel color picker";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: rec {
        zelo = pkgs.callPackage ./package.nix { };
        default = zelo;
      });

      apps = forAllSystems (pkgs: rec {
        zelo = {
          type = "app";
          program = nixpkgs.lib.getExe self.packages.${pkgs.stdenv.hostPlatform.system}.zelo;
        };
        default = zelo;
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell { packages = [ pkgs.python3 ]; };
      });

      overlays.default = final: _prev: {
        zelo = final.callPackage ./package.nix { };
      };

      nixosModules.default = import ./module.nix self;
      nixosModules.zelo = self.nixosModules.default;
    };
}
