# zelo

Zelotes F-36 (Areson `25a7:fa68`) RGB panel color picker for Linux.

```
zelo -c red                  # color: red green blue cyan magenta white yellow orange | #rrggbb
zelo -m rainbow              # mode: off static rainbow rainbow_clippy (+ breathing)
zelo -c "#ff8800" -s 3 -l 4  # speed / brightness
zelo                         # show saved state
zelo -n -c red               # dry run, print the HID packet
```

## NixOS

```nix
# flake.nix
inputs.zelo.url = "github:<you>/zelo";   # or path:/home/you/zelo

# configuration.nix
imports = [ inputs.zelo.nixosModules.default ];
programs.zelo.enable = true;             # installs zelo + udev rule (uaccess)
```

Try without installing: `nix run github:<you>/zelo -- -c red`
(needs the udev rule once, otherwise `sudo` or "permission denied").

Packet format and how it was found: see the header of `zelo`.
