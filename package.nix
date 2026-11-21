{ lib, stdenvNoCC, python3 }:

stdenvNoCC.mkDerivation {
  pname = "zelo";
  version = "0.1.0";

  dontUnpack = true;
  dontBuild = true;

  # python3 in buildInputs so patchShebangs points the script at the store python
  buildInputs = [ python3 ];

  installPhase = ''
    runHook preInstall
    install -Dm755 ${./zelo} $out/bin/zelo
    runHook postInstall
  '';

  meta = {
    description = "Zelotes F-36 RGB panel color picker";
    mainProgram = "zelo";
    platforms = lib.platforms.linux;
  };
}
