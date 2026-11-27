"""zelo - Zelotes F-36 command line.

    zelo -c red                  set color            (red green blue cyan magenta white yellow orange | #rrggbb)
    zelo -m rainbow              set mode             (off static rainbow rainbow_clippy breathing)
    zelo -c "#ff8800" -s 3 -l 4  speed / brightness
    zelo                         show current LED state
    zelo -w 10 -c red            keep re-sending up to 10 s until the (sleeping) mouse acknowledges
    zelo info                    decode the main config block
    zelo rate [125|250|500] [--bt HZ]   polling rate
    zelo dpi [800 1200 ...] [--stage N] [--count N]   DPI stages
    zelo keys                    show what every button does (numbers as in the vendor picture)
    zelo bind 6 dpi-             rebind button 6: left right middle back forward
                                 dpi-loop dpi+ dpi- rgb none | key:ctrl+shift+t
                                 (button 8 = BT/2.4G switch, cannot be rebound)
    zelo restore FILE --yes      write back a backup made by zelo
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
        epilog="other commands: info dump read rate dpi keys bind restore")
    ap.add_argument("-c", "--color", type=parse_color, metavar="COLOR",
                    help="red,green,blue,cyan,magenta,white,yellow,orange or #rrggbb")
    ap.add_argument("-m", "--mode", choices=MODES, metavar="MODE",
                    help="off,static,rainbow,rainbow_clippy (+ breathing)")
    ap.add_argument("-s", "--speed", type=int, metavar="N", help="effect speed")
    ap.add_argument("-l", "--light", type=int, metavar="N", help="brightness")
    add_common(ap)
    ap.add_argument("-n", "--dry-run", action="store_true", help="print packet, don't send")
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
            acked = mouse.write(P.ADDR_LED, raw, wait=a.wait)
        except P.ZeloError as e:
            sys.exit(f"zelo: {e}")
        if a.verbose:
            print(f"sent {fmt_state(st)}; acknowledged: {acked}", file=sys.stderr)
        if not acked and (a.verbose or a.wait > 0):
            print(ASLEEP_HINT, file=sys.stderr)
        save_cache(st)
    finally:
        if mouse is not None:
            mouse.close()


# --- backups ----------------------------------------------------------------

BACKUP_DIR = os.path.join(os.path.dirname(STATE_PATH), "backups")


def backup(mouse):
    """Save the 0x00..0x3b config block before touching it. Returns the file path."""
    import time
    data = mouse.read_config()
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, time.strftime("%Y%m%d-%H%M%S") + ".bin")
    with open(path, "wb") as f:
        f.write(data)
    return path


def read_cfg_or_die(mouse):
    try:
        data = mouse.read_config()
    except P.ZeloError as e:
        sys.exit(f"zelo: {e}")
    return data, P.decode_config(data)


def fmt_rate(byte):
    if byte is None:
        return "unreadable (checksum mismatch)"
    g, b = P.decode_rate(byte)
    f = lambda hz: f"{hz} Hz" if hz else "-"  # noqa: E731
    return f"2.4G {f(g)}, BT {f(b)}  (byte 0x{byte:02x})"


BUTTON_BITS = {v: k for k, v in P.MOUSE_BUTTONS.items()}
FUNC_NAMES = {v: k for k, v in P.FUNCTIONS.items()}


def entry_name(k):
    """Name of a matrix entry without reading the macro area."""
    if k["type"] == P.TYPE_BUTTON and k["code"] in BUTTON_BITS:
        return f"mouse {BUTTON_BITS[k['code']]}"
    if (k["type"], k["code"]) in FUNC_NAMES:
        return FUNC_NAMES[(k["type"], k["code"])]
    if k["type"] == P.TYPE_MACRO:
        return f"keyboard/macro (slot {k['code']})"
    return "unknown"


def fmt_key(k):
    return f"{k['type']:02x} {k['code']:02x} {k['param']:02x}  {entry_name(k)}"


def fmt_dpi_slot(d):
    if d["dpi"]:
        return f"{d['dpi']} DPI"
    return f"code X=0x{d['x']:02x} Y=0x{d['y']:02x}"


ASLEEP_HINT = ("zelo: no acknowledgement from the mouse (asleep?). The dongle may still "
               "deliver the change when it wakes up; move the mouse, or use -w SECONDS to keep retrying.")


def add_common(ap):
    ap.add_argument("-d", "--device", metavar="PATH", help="force /dev/hidrawN")
    ap.add_argument("-w", "--wait", type=float, default=0.0, metavar="SEC",
                    help="re-send until acknowledged, up to SEC seconds (wireless mouse asleep)")
    ap.add_argument("-v", "--verbose", action="store_true")


def safe_write(mouse, what, fn, wait=0.0, verbose=False):
    """Back up the config block, run fn() (returns acked: bool), report."""
    try:
        path = backup(mouse)
        acked = fn()
    except P.ZeloError as e:
        sys.exit(f"zelo: {e}")
    print(f"{what}  (backup: {path})")
    if not acked and (verbose or wait > 0):
        print(ASLEEP_HINT, file=sys.stderr)


# --- zelo info --------------------------------------------------------------

def cmd_info(argv):
    ap = argparse.ArgumentParser(prog="zelo info", description="decode the main config block")
    ap.add_argument("-d", "--device", metavar="PATH")
    a = ap.parse_args(argv)
    with open_mouse(a.device) as m:
        data, c = read_cfg_or_die(m)

    print(f"polling rate: {fmt_rate(c['rate_byte'])}")
    n, lv = c["stages"], c["level"]
    print(f"DPI stages:   {n if n is not None else '?'} enabled, "
          f"current level {lv if lv is not None else '?'}"
          + (f" (stage {lv + 1} if 0-based)" if lv is not None else ""))
    for i, d in enumerate(c["dpi"]):
        print(f"  stage {i + 1}: {fmt_dpi_slot(d)}  chk {'ok' if d['ok'] else 'BAD'}")
    print(f"LED:          {fmt_state(c['led'])}" if c["led"] else "LED:          checksum MISMATCH")
    print("buttons (key matrix, slot: type code param):")
    for i, k in enumerate(c["keys"]):
        print(f"  slot {i}: {fmt_key(k)}  chk {'ok' if k['ok'] else 'BAD'}  [button {P.SLOT_BUTTON[i]}]")
    print("\nraw 0x00..0x3b:")
    print(hexdump(data))


BUTTON_NAMES = {1: "left button", 2: "right button", 3: "wheel button", 4: "side: forward",
                5: "side: back", 6: "DPI", 7: "extra: lighting", 8: "BT/2.4G switch"}


def describe_slot(mouse, i, k):
    """Human description of slot i, reading the macro area for keyboard bindings."""
    if i == P.BT_SWITCH_SLOT:
        return "BT / 2.4G switch (fixed)"
    if k["type"] == P.TYPE_MACRO:
        try:
            dec = P.decode_key_macro(mouse.read_macro_header(k["code"]))
        except P.ZeloError:
            dec = None
        return f"key:{P.format_keystroke(*dec)}" if dec else f"macro (slot {k['code']}, unknown format)"
    return entry_name(k)


def cmd_keys(argv):
    ap = argparse.ArgumentParser(prog="zelo keys", description="show what every button does")
    ap.add_argument("-d", "--device", metavar="PATH")
    a = ap.parse_args(argv)
    with open_mouse(a.device) as m:
        _, c = read_cfg_or_die(m)
        for b in sorted(P.BUTTON_SLOT):
            i = P.BUTTON_SLOT[b]
            k = c["keys"][i]
            ok = "" if k["ok"] else "  [checksum BAD]"
            print(f"{b}  {BUTTON_NAMES[b]:<17} {describe_slot(m, i, k):<26} "
                  f"(factory: {P.BUTTON_FACTORY[b]}){ok}")


# --- zelo bind --------------------------------------------------------------

def cmd_bind(argv):
    from . import api  # imported here: api itself imports this module
    ap = argparse.ArgumentParser(prog="zelo bind", description="rebind a button")
    ap.add_argument("button", type=int, help="button number 1..8 as in the vendor picture (see `zelo keys`)")
    ap.add_argument("action", help="left right middle back forward dpi-loop dpi+ dpi- rgb none | key:<combo>")
    ap.add_argument("--slot", dest="raw", action="store_true",
                    help="treat the number as a raw memory slot 0..7 instead of a button number")
    ap.add_argument("--force", action="store_true",
                    help="overwrite a macro in an unknown format / the BT-2.4G switch slot")
    add_common(ap)
    a = ap.parse_args(argv)

    if a.raw:
        slot = a.button
    else:
        slot = P.BUTTON_SLOT.get(a.button)
    if slot is None or not 0 <= slot < P.KEY_SLOTS:
        sys.exit("zelo: button must be 1..8" if not a.raw else "zelo: slot must be 0..7")

    with open_mouse(a.device) as m:
        try:
            res = api.bind(m, slot, a.action, wait=a.wait, force=a.force)
        except (ValueError, P.ZeloError) as e:
            sys.exit(f"zelo: {e}")
    what = f"slot {slot}" if a.raw else f"button {a.button}"
    print(f"{what} -> {a.action}  (backup: {res['backup']})")
    if not res["acked"] and (a.verbose or a.wait > 0):
        print(ASLEEP_HINT, file=sys.stderr)
    print("verified: " + ("yes" if res["verified"]
                          else "NO - read-back differs (mouse asleep? try again with -w 10)"))


# --- zelo rate --------------------------------------------------------------

def cmd_rate(argv):
    ap = argparse.ArgumentParser(prog="zelo rate", description="polling rate")
    ap.add_argument("hz", nargs="?", type=int, help="2.4G/wired rate: 125, 250 or 500")
    ap.add_argument("--bt", type=int, metavar="HZ", help="Bluetooth rate: 125, 250 or 500")
    ap.add_argument("--force", action="store_true", help="allow 1000 Hz (the driver does not read it back)")
    add_common(ap)
    a = ap.parse_args(argv)
    allowed = {125, 250, 500} | ({1000} if a.force else set())
    for v in (a.hz, a.bt):
        if v is not None and v not in allowed:
            sys.exit(f"zelo: rate must be one of {sorted(allowed)}")
    with open_mouse(a.device) as m:
        _, c = read_cfg_or_die(m)
        cur = c["rate_byte"]
        if cur is None:
            sys.exit("zelo: current rate byte failed its checksum, refusing to write")
        if a.hz is None and a.bt is None:
            print(fmt_rate(cur))
            return
        new = P.encode_rate(a.hz, a.bt, cur)
        safe_write(m, f"polling rate -> {fmt_rate(new)}",
                   lambda: m.write_rate_byte(new, wait=a.wait), a.wait, a.verbose)


# --- zelo dpi ---------------------------------------------------------------

def cmd_dpi(argv):
    ap = argparse.ArgumentParser(prog="zelo dpi", description="DPI stages")
    ap.add_argument("values", nargs="*", type=int, help="DPI for stage 1, 2, ... (400..2400 step 100)")
    ap.add_argument("--stage", type=int, metavar="N", help="select current stage (1-based)")
    ap.add_argument("--count", type=int, metavar="N", help="number of enabled stages (1..5)")
    add_common(ap)
    a = ap.parse_args(argv)

    if len(a.values) > P.DPI_SLOTS:
        sys.exit(f"zelo: at most {P.DPI_SLOTS} stages")
    for v in a.values:
        if v not in P.SENSOR_DPI:
            sys.exit(f"zelo: {v} DPI not supported; use 400..2400 in steps of 100")
    if a.count is not None and not 1 <= a.count <= P.DPI_SLOTS:
        sys.exit(f"zelo: --count must be 1..{P.DPI_SLOTS}")

    with open_mouse(a.device) as m:
        _, c = read_cfg_or_die(m)
        if not a.values and a.stage is None and a.count is None:
            n, lv = c["stages"], c["level"]
            for i, d in enumerate(c["dpi"]):
                mark = "*" if lv is not None and i == lv else " "
                off = "" if n is None or i < n else "  (disabled)"
                print(f"{mark} stage {i + 1}: {fmt_dpi_slot(d)}{off}")
            return
        if c["stages"] is None or c["level"] is None:
            sys.exit("zelo: stage count/level failed their checksum, refusing to write")

        count = a.count if a.count is not None else (len(a.values) if a.values else c["stages"])
        stage = (a.stage - 1) if a.stage is not None else c["level"]
        if not 0 <= stage < count:
            sys.exit(f"zelo: stage must be 1..{count}")

        def apply():
            ok = True
            for i, v in enumerate(a.values):
                ok = m.write_dpi(i, v, wait=a.wait) and ok
            if count != c["stages"]:
                ok = m.write_stages(count, wait=a.wait) and ok
            if stage != c["level"]:
                ok = m.write_level(stage, wait=a.wait) and ok
            return ok

        safe_write(m, f"dpi stages updated ({count} enabled, current stage {stage + 1})",
                   apply, a.wait, a.verbose)


# --- zelo restore -----------------------------------------------------------

def cmd_restore(argv):
    ap = argparse.ArgumentParser(prog="zelo restore", description="write back a config backup")
    ap.add_argument("file")
    ap.add_argument("--yes", action="store_true", help="really write")
    add_common(ap)
    a = ap.parse_args(argv)
    with open(a.file, "rb") as f:
        data = f.read()
    if len(data) != P.CONFIG_LEN or not P.config_is_sane(data):
        sys.exit(f"zelo: {a.file} is not a valid config image ({P.CONFIG_LEN} bytes, all checksums ok)")
    if not a.yes:
        print(hexdump(data))
        print("\nrun again with --yes to write this to the mouse")
        return
    with open_mouse(a.device) as m:
        safe_write(m, "config restored", lambda: m.write(0, data, wait=a.wait), a.wait, a.verbose)


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


COMMANDS = {"info": cmd_info, "dump": cmd_dump, "read": cmd_read, "rate": cmd_rate,
            "dpi": cmd_dpi, "keys": cmd_keys, "bind": cmd_bind, "restore": cmd_restore}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in COMMANDS:
        return COMMANDS[argv[0]](argv[1:])
    return cmd_set(argv)


if __name__ == "__main__":
    main()
