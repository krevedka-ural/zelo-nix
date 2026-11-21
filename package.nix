{ lib, python3Packages }:

python3Packages.buildPythonApplication {
  pname = "zelo";
  version = "0.2.0";
  pyproject = true;

  src = lib.cleanSource ./.;

  build-system = [ python3Packages.setuptools ];

  # stdlib only; tests (fake mouse): python3 -m unittest discover -s tests
  doCheck = false;
  pythonImportsCheck = [ "zelo" ];

  meta = {
    description = "Zelotes F-36 mouse control (RGB panel, memory dump)";
    mainProgram = "zelo";
    platforms = lib.platforms.linux;
  };
}
