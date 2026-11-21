"""zelo - Zelotes F-36 command line.

    zelo -c red                  set color            (red green blue cyan magenta white yellow orange | #rrggbb)
    zelo -m rainbow              set mode             (off static rainbow rainbow_clippy breathing)
    zelo -c "#ff8800" -s 3 -l 4  speed / brightness
    zelo                         show current LED state
    zelo info                    decode the main config block
    zelo dump -o backup.bin      hexdump / save mouse memory (read-only)
    zelo read 0x35 7             read raw bytes
"""
import argparse
import json
import os
import sys

from . import protocol as P

COLORS = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "white": (255, 255, 255),
    "yellow": (255, 255, 0),
    "yelow": (255, 255, 0),  # sic, accepted as in the draft
    "orange": (255, 128, 0),
}

# mode byte = 0-based index of the radio button in the original UI.
# NOTE: order is inferred from the driver, not verified on the device.
MODES = {
    "off": 4,
    "static": 1,           # "Steady"
    "rainbow": 2,          # "Colorful"
    "rainbow_clippy": 0,   # "Neon" (guess)
    "breathing": 3,        # extra, "Respiration"
}
MODE_NAMES = {v: k for k, v in MODES.items()}
OFF = MODES["off"]

STATE_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "zelo", "state.json")
DEFAULT = {"mode": MODES["static"], "color": [255, 255, 255], "speed": 2, "light": 2}


# --- helpers ----------------------------------------------------------------

def load_cache():
    st = dict(DEFAULT)
    try:
        with open(STATE_PATH) as f:
            st.update(json.load(f))
    except (OSError, ValueError):
        pass
    if isinstance(st["mode"], str):  # older state.json stored names
        st["mode"] = MODES.get(st["mode"], DEFAULT["mode"])
    return st


def save_cache(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(st, f)


def parse_color(s):
    s = s.lower()
    if s in COLORS:
        return list(COLORS[s])
    h = s.lstrip("#")
    if len(h) == 6:
        try:
            return [int(h[i:i + 2], 16) for i in (0, 2, 4)]
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(
        f"unknown color '{s}' (names: {', '.join(k for k in COLORS if k != 'yelow')}, or #rrggbb)")


def fmt_state(st):
    r, g, b = st["color"]
    return (f"mode={st['mode']} ({MODE_NAMES.get(st['mode'], '?')}) "
            f"color=#{r:02x}{g:02x}{b:02x} speed={st['speed']} light={st['light']}")


def open_mouse(path):
    try:
        return P.Mouse(path=path)
    except P.ZeloError as e:
        sys.exit(f"zelo: {e}")


def hexdump(data, base=0):
    lines = []
    for i in range(0, len(data), 16):
        row = data[i:i + 16]
        hx = " ".join(f"{x:02x}" for x in row)
        asc = "".join(chr(x) if 32 <= x < 127 else "." for x in row)
        lines.append(f"{base + i:04x}: {hx:<47}  |{asc}|")
    return "\n".join(lines)


# --- zelo [-c ..] [-m ..] ---------------------------------------------------

def cmd_set(argv):
    ap = argparse.ArgumentParser(
        prog="zelo", description="Zelotes F-36 RGB panel color picker",
        epilog="other commands: zelo info | zelo dump | zelo read")
    ap.add_argument("-c", "--color", type=parse_color, metavar="COLOR",
                    help="red,green,blue,cyan,magenta,white,yellow,orange or #rrggbb")
    ap.add_argument("-m", "--mode", choices=MODES, metavar="MODE",
                    help="off,static,rainbow,rainbow_clippy (+ breathing)")
    ap.add_argument("-s", "--speed", type=int, metavar="N", help="effect speed")
    ap.add_argument("-l", "--light", type=int, metavar="N", help="brightness")
    ap.add_argument("-d", "--device", metavar="PATH", help="force /dev/hidrawN")
    ap.add_argument("-n", "--dry-run", action="store_true", help="print packet, don't send")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    changing = any(v is not None for v in (a.color, a.mode, a.speed, a.light))
    mouse = None if (a.dry_run and changing) else open_mouse(a.device)
    try:
        st, src = None, "cache"
        if mouse is not None:
            try:
                st = mouse.read_led()
                src = "mouse"
            except P.ZeloError as e:
                if a.verbose:
                    print(f"zelo: could not read LED state from mouse ({e})", file=sys.stderr)
            if st is None and a.verbose and src == "cache":
                print("zelo: LED block unreadable/invalid, using cached state", file=sys.stderr)
        st = st or load_cache()
        if a.verbose:
            print(f"base state ({src}): {fmt_state(st)}", file=sys.stderr)

        if not changing:
            print(fmt_state(st))
            return

        if a.color is not None:
            st["color"] = a.color
            # picking a color while the panel is off should light it up
            if a.mode is None and st["mode"] == OFF:
                st["mode"] = MODES["static"]
        if a.mode is not None:
            st["mode"] = MODES[a.mode]
        if a.speed is not None:
            st["speed"] = a.speed
        if a.light is not None:
            st["light"] = a.light

        raw = P.encode_led(st["mode"], st["color"], st["speed"], st["light"])
        if a.dry_run:
            print(P.build_request(P.CMD_WRITE, P.ADDR_LED, raw).hex(" "))
            return
        try:
            acked = mouse.write(P.ADDR_LED, raw)
        except P.ZeloError as e:
            sys.exit(f"zelo: {e}")
        if a.verbose:
            print(f"sent {fmt_state(st)}; acknowledged: {acked}", file=sys.stderr)
        save_cache(st)
    finally:
        if mouse is not None:
            mouse.close()


# --- zelo info --------------------------------------------------------------

def _records(data, base, size, count):
    for i in range(count):
        rec = data[base + i * size: base + (i + 1) * size]
        ok = P.checksum(rec[:-1]) == rec[-1]
        yield i, rec, ok


def cmd_info(argv):
    ap = argparse.ArgumentParser(prog="zelo info", description="decode the main config block")
    ap.add_argument("-d", "--device", metavar="PATH")
    a = ap.parse_args(argv)
    with open_mouse(a.device) as m:
        try:
            data = m.read(0x00, 0x3C)
        except P.ZeloError as e:
            sys.exit(f"zelo: {e}")
    led = P.decode_led(data[P.ADDR_LED:P.ADDR_LED + P.LED_LEN])
    print(f"LED @0x35: {fmt_state(led)} [checksum ok]" if led
          else f"LED @0x35: {data[0x35:0x3c].hex(' ')} [checksum MISMATCH]")
    print("\nDPI stages @0x06 (5 x 3 B; layout not verified yet):")
    for i, rec, ok in _records(data, 0x06, 3, 5):
        print(f"  #{i + 1}: {rec.hex(' ')}  chk {'ok' if ok else 'no'}")
    print("\nDPI colors @0x15 (8 x 4 B; layout not verified yet):")
    for i, rec, ok in _records(data, 0x15, 4, 8):
        print(f"  #{i + 1}: {rec.hex(' ')}  chk {'ok' if ok else 'no'}")
    print("\nraw 0x00..0x3b:")
    print(hexdump(data))


# --- zelo dump / read -------------------------------------------------------

def cmd_dump(argv):
    ap = argparse.ArgumentParser(prog="zelo dump", description="read mouse memory (read-only)")
    ap.add_argument("--start", type=lambda s: int(s, 0), default=0x000)
    ap.add_argument("--end", type=lambda s: int(s, 0), default=0x100, help="exclusive (default 0x100)")
    ap.add_argument("-o", "--output", metavar="FILE", help="also save raw bytes")
    ap.add_argument("-d", "--device", metavar="PATH")
    a = ap.parse_args(argv)
    if a.end <= a.start:
        sys.exit("zelo: --end must be greater than --start")
    with open_mouse(a.device) as m:
        try:
            data = m.read(a.start, a.end - a.start)
        except P.ZeloError as e:
            sys.exit(f"zelo: {e}")
    print(hexdump(data, a.start))
    if a.output:
        with open(a.output, "wb") as f:
            f.write(data)
        print(f"saved {len(data)} bytes to {a.output}", file=sys.stderr)


def cmd_read(argv):
    ap = argparse.ArgumentParser(prog="zelo read", description="read raw bytes")
    ap.add_argument("addr", type=lambda s: int(s, 0))
    ap.add_argument("length", type=lambda s: int(s, 0))
    ap.add_argument("-d", "--device", metavar="PATH")
    a = ap.parse_args(argv)
    with open_mouse(a.device) as m:
        try:
            print(m.read(a.addr, a.length).hex(" "))
        except P.ZeloError as e:
            sys.exit(f"zelo: {e}")


COMMANDS = {"info": cmd_info, "dump": cmd_dump, "read": cmd_read}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in COMMANDS:
        return COMMANDS[argv[0]](argv[1:])
    return cmd_set(argv)


if __name__ == "__main__":
    main()
