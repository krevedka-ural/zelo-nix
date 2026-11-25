{ lib, python3Packages }:

python3Packages.buildPythonApplication {
  pname = "zelo";
  version = "0.3.0";
  pyproject = true;

  src = lib.cleanSource ./.;

  build-system = [ python3Packages.setuptools ];

  # CLI is stdlib only; the Qt launcher lives in gui.nix
  postInstall = ''
    rm -f $out/bin/zelo-gui
  '';

  # tests (fake mouse): python3 -m unittest discover -s tests
  doCheck = false;
  pythonImportsCheck = [ "zelo" ];

  meta = {
    description = "Zelotes F-36 mouse control (RGB, DPI, polling rate, buttons)";
    mainProgram = "zelo";
    platforms = lib.platforms.linux;
  };
}
