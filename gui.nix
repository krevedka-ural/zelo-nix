{ lib, python3Packages, qt6 }:

# Same source as package.nix, plus PySide6 and the Qt wrapper. Contains both
# `zelo-gui` and the `zelo` command, so install this OR the plain package, not both.
python3Packages.buildPythonApplication {
  pname = "zelo-gui";
  version = "0.3.0";
  pyproject = true;

  src = lib.cleanSource ./.;

  build-system = [ python3Packages.setuptools ];
  dependencies = [ python3Packages.pyside6 ];

  nativeBuildInputs = [ qt6.wrapQtAppsHook ];
  buildInputs = [ qt6.qtbase qt6.qtwayland ];

  # python apps: wrap through makeWrapperArgs instead of the Qt hook's own wrapping
  dontWrapQtApps = true;
  preFixup = ''
    makeWrapperArgs+=("''${qtWrapperArgs[@]}")
  '';

  doCheck = false;

  meta = {
    description = "Qt interface for the Zelotes F-36 mouse";
    mainProgram = "zelo-gui";
    platforms = lib.platforms.linux;
  };
}
