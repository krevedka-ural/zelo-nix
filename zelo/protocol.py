"""Protocol layer for the Zelotes F-36 mouse (Areson 25a7:fa68).

Reverse-engineered from the vendor driver (OemDrv.exe). Everything is a HID
*feature report* with report ID 0x08, 15 bytes long:

    [0]  0x08          report ID
    [1]  cmd           0x03 = write memory, 0x04 = read memory
    [2]  0x00
    [3]  addr high
    [4]  addr low
    [5]  length        (<= 8 per packet)
    [6.] data          (write only)
    [14] checksum      (0x55 - sum(bytes 0..13)) & 0xff

The mouse answers on an input report whose payload mirrors the request:
    cmd, 0x00, addr high, addr low, length, data...
(the driver treats it as a 16-byte message; whether the kernel hands us a
leading report-ID byte depends on the descriptor, so both layouts are accepted).

Memory map (confirmed by the driver's own debug strings):
    0x00,0x01  polling rate byte + checksum   (low nibble 2.4G, high nibble BT;
                                               codes 1=1000Hz 2=500 4=250 8=125)
    0x02,0x03  number of DPI stages + checksum
    0x04,0x05  current DPI level + checksum
    0x06..0x14 DPI stages, 5 x [X, Y, chk]     (X/Y = sensor code, see SENSOR_DPI)
    0x15..0x34 key matrix, 8 x [type, code, param, chk]
    0x35..0x3b LED block: mode, R, G, B, speed, light, checksum
    0x200+     macro slots, 0x180 bytes each (slot n at 0x200 + n*0x180); a keyboard
               binding is a matrix entry [04, n, 01] pointing at macro slot n:
                 +0x00..0x1c  name area (zeros)
                 +0x1d        0x52, +0x1e 0x03   (keystroke macro header)
                 +0x1f        event count n
                 +0x20        n events x 4 bytes [op, d1, d2, 0x0f]
                              op 0x80/0x40 modifier down/up (d1 = HID modifier bit)
                                 0x81/0x41 key down/up      (d1 = HID usage)
                 then 1 checksum byte over (count + events)
Every record ends with chk = (0x55 - sum(record bytes)) & 0xff.

Transport is plain /dev/hidrawN + ioctl(HIDIOCSFEATURE), stdlib only.
"""
import fcntl
import glob
import os
import select
import time

VID, PID = 0x25A7, 0xFA68

REPORT_ID = 0x08
REPORT_LEN = 15
CMD_WRITE = 0x03
CMD_READ = 0x04
CHUNK = 8  # max data bytes per packet (13-byte payload - 5 header bytes)

ADDR_LED = 0x0035
LED_LEN = 7

ADDR_RATE = 0x00
ADDR_STAGES = 0x02
ADDR_LEVEL = 0x04
ADDR_DPI = 0x06
ADDR_KEYS = 0x15
CONFIG_LEN = 0x3C        # 0x00..0x3b
DPI_SLOTS = 5
KEY_SLOTS = 8

# polling rate nibble codes
RATE_CODES = {1000: 1, 500: 2, 250: 4, 125: 8}
RATE_HZ = {v: k for k, v in RATE_CODES.items()}

# Sensor 0x3212 (PixArt PAW3212 class): DPI -> resolution code, from the driver's table.
SENSOR_DPI = {
    400: 0x0B, 500: 0x0D, 600: 0x10, 700: 0x12, 800: 0x15, 900: 0x17,
    1000: 0x1A, 1100: 0x1D, 1200: 0x1F, 1300: 0x22, 1400: 0x25, 1500: 0x27,
    1600: 0x2A, 1700: 0x2D, 1800: 0x2F, 1900: 0x32, 2000: 0x34, 2100: 0x37,
    2200: 0x3A, 2300: 0x3C, 2400: 0x3F,
}
DPI_FROM_CODE = {v: k for k, v in SENSOR_DPI.items()}

# --- buttons / functions (matrix entries) ---------------------------------------

MOUSE_BUTTONS = {"left": 0x01, "right": 0x02, "middle": 0x04, "back": 0x08, "forward": 0x10}
FUNCTIONS = {                     # name -> (type, code)
    "none": (0x00, 0x00),
    "dpi-loop": (0x02, 0x01),
    "dpi+": (0x02, 0x02),
    "dpi-": (0x02, 0x03),
    "rgb": (0x05, 0x00),
}
TYPE_BUTTON, TYPE_MACRO = 0x01, 0x04

# Physical buttons as numbered in the vendor's picture (and Cfg.ini K1..K8) -> matrix slot.
# Slot = (id from Cfg.ini) - 1, cross-checked against a real memory dump.
BUTTON_SLOT = {1: 2, 2: 3, 3: 0, 4: 7, 5: 5, 6: 1, 7: 4, 8: 6}
BUTTON_FACTORY = {1: "left", 2: "right", 3: "middle", 4: "forward", 5: "back",
                  6: "dpi-loop", 7: "rgb", 8: "bt-switch"}
SLOT_BUTTON = {slot: btn for btn, slot in BUTTON_SLOT.items()}
FACTORY_SLOTS = {BUTTON_SLOT[b]: act for b, act in BUTTON_FACTORY.items()}
BT_SWITCH_BUTTON = 8      # BT <-> 2.4G switch: handled by the firmware, not rebindable
BT_SWITCH_SLOT = BUTTON_SLOT[BT_SWITCH_BUTTON]

ADDR_MACRO = 0x200
MACRO_SIZE = 0x180
MACRO_HDR = 0x1D          # marker offset inside a slot
KEY_MACRO_MARK = (0x52, 0x03)

MODIFIERS = {"ctrl": 0x01, "shift": 0x02, "alt": 0x04, "win": 0x08, "super": 0x08, "gui": 0x08}
_MOD_DOWN_ORDER = (0x01, 0x02, 0x08, 0x04)   # the driver's order
_MOD_UP_ORDER = (0x04, 0x08, 0x02, 0x01)

# USB HID keyboard usages the vendor driver accepts (from its 104-entry table)
DRIVER_USAGES = frozenset(
    list(range(0x04, 0x28)) + [0x28, 0x29, 0x2A, 0x2B, 0x2C, 0x2D, 0x2E, 0x2F, 0x30, 0x31,
                               0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39]
    + list(range(0x3A, 0x46)) + list(range(0x46, 0x4F)) + list(range(0x4F, 0x53))
    + list(range(0x53, 0x64)) + [0x65] + list(range(0xE0, 0xE8)))

KEYS = {}
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    KEYS[_c] = 0x04 + _i
for _i, _c in enumerate("1234567890"):
    KEYS[_c] = 0x1E + _i
for _i in range(12):
    KEYS[f"f{_i + 1}"] = 0x3A + _i
for _i in range(9):
    KEYS[f"kp{_i + 1}"] = 0x59 + _i
KEYS.update({
    "enter": 0x28, "esc": 0x29, "backspace": 0x2A, "tab": 0x2B, "space": 0x2C,
    "minus": 0x2D, "equal": 0x2E, "lbracket": 0x2F, "rbracket": 0x30, "backslash": 0x31,
    "semicolon": 0x33, "quote": 0x34, "grave": 0x35, "comma": 0x36, "period": 0x37,
    "slash": 0x38, "capslock": 0x39, "printscreen": 0x46, "scrolllock": 0x47,
    "pause": 0x48, "insert": 0x49, "home": 0x4A, "pageup": 0x4B, "delete": 0x4C,
    "end": 0x4D, "pagedown": 0x4E, "arrow-right": 0x4F, "arrow-left": 0x50,
    "arrow-down": 0x51, "arrow-up": 0x52, "numlock": 0x53, "kp/": 0x54, "kp*": 0x55,
    "kp-": 0x56, "kp+": 0x57, "kpenter": 0x58, "kp0": 0x62, "kp.": 0x63, "menu": 0x65,
})
KEY_NAMES = {v: k for k, v in KEYS.items()}


class ZeloError(Exception):
    pass


def checksum(data):
    return (0x55 - sum(data)) & 0xFF


def build_request(cmd, addr, payload=b"", length=None):
    n = len(payload) if length is None else length
    pkt = [REPORT_ID, cmd, 0x00, (addr >> 8) & 0xFF, addr & 0xFF, n] + list(payload)
    if len(pkt) > REPORT_LEN - 1:
        raise ValueError("payload too long for one packet")
    pkt += [0] * (REPORT_LEN - 1 - len(pkt))
    pkt.append(checksum(pkt))
    return bytes(pkt)


def _matcher(cmd, addr, n, need_data=True):
    hi, lo = (addr >> 8) & 0xFF, addr & 0xFF

    def match(rep):
        for o in (0, 1):  # with / without a leading report-ID byte
            if (len(rep) >= o + 5 and rep[o] == cmd and rep[o + 1] == 0
                    and rep[o + 2] == hi and rep[o + 3] == lo
                    and (not need_data or len(rep) >= o + 5 + n)):
                return bytes(rep[o + 5:o + 5 + n])
        return None

    return match


# --- LED block --------------------------------------------------------------

def encode_led(mode, rgb, speed, light):
    body = [mode, *rgb, speed, light]
    body.append(checksum(body))
    return bytes(body)


def decode_led(raw):
    """-> dict or None if the checksum does not match."""
    if len(raw) != LED_LEN or checksum(raw[:6]) != raw[6]:
        return None
    return {"mode": raw[0], "color": list(raw[1:4]), "speed": raw[4], "light": raw[5]}


# --- single-byte / record helpers ---------------------------------------------

def encode_pair(v):
    return bytes([v, checksum([v])])


def decode_pair(raw):
    """[value, chk] -> value or None."""
    return raw[0] if len(raw) == 2 and checksum(raw[:1]) == raw[1] else None


def encode_rate(g24=None, bt=None, current=0):
    """Rate byte from Hz values; a nibble left as None keeps its bits from `current`."""
    lo = current & 0x0F if g24 is None else RATE_CODES[g24]
    hi = (current >> 4) & 0x0F if bt is None else RATE_CODES[bt]
    return (hi << 4) | lo


def decode_rate(byte):
    """-> (g24_hz, bt_hz); None for an unknown/unused nibble."""
    return RATE_HZ.get(byte & 0x0F), RATE_HZ.get(byte >> 4)


def encode_dpi(dpi):
    if dpi not in SENSOR_DPI:
        raise ValueError(f"DPI {dpi} not supported (400..2400 step 100)")
    c = SENSOR_DPI[dpi]
    return bytes([c, c, checksum([c, c])])


def encode_key(kind, code, param=0):
    rec = [kind, code, param]
    return bytes(rec + [checksum(rec)])


def decode_config(data):
    """Decode bytes 0x00..0x3b into a dict; every field carries its own checksum verdict."""
    if len(data) < CONFIG_LEN:
        raise ValueError("config block too short")
    cfg = {}
    cfg["rate_byte"] = decode_pair(data[0:2])
    cfg["stages"] = decode_pair(data[2:4])
    cfg["level"] = decode_pair(data[4:6])
    cfg["dpi"] = []
    for i in range(DPI_SLOTS):
        rec = data[ADDR_DPI + 3 * i: ADDR_DPI + 3 * i + 3]
        ok = checksum(rec[:2]) == rec[2]
        cfg["dpi"].append({"x": rec[0], "y": rec[1], "ok": ok,
                           "dpi": DPI_FROM_CODE.get(rec[0]) if rec[0] == rec[1] else None})
    cfg["keys"] = []
    for i in range(KEY_SLOTS):
        rec = data[ADDR_KEYS + 4 * i: ADDR_KEYS + 4 * i + 4]
        cfg["keys"].append({"type": rec[0], "code": rec[1], "param": rec[2],
                            "ok": checksum(rec[:3]) == rec[3]})
    cfg["led"] = decode_led(data[ADDR_LED:ADDR_LED + LED_LEN])
    return cfg


def config_is_sane(data):
    """True if every checksummed field in a 0x3c-byte image verifies (used by restore)."""
    c = decode_config(data)
    return (c["rate_byte"] is not None and c["stages"] is not None and c["level"] is not None
            and all(d["ok"] for d in c["dpi"]) and all(k["ok"] for k in c["keys"])
            and c["led"] is not None)


# --- keyboard macros ----------------------------------------------------------

def parse_keystroke(text):
    """'ctrl+shift+t' -> (modifier_mask, [usage, ...]); at most 3 non-modifier keys."""
    mods, keys = 0, []
    for part in text.lower().split("+"):
        part = part.strip()
        if part in MODIFIERS:
            mods |= MODIFIERS[part]
        elif part in KEYS:
            keys.append(KEYS[part])
        else:
            raise ValueError(f"unknown key '{part}'")
    if len(keys) > 3:
        raise ValueError("at most 3 keys besides modifiers")
    if not keys and not mods:
        raise ValueError("empty keystroke")
    return mods, keys


def format_keystroke(mods, keys):
    names = [n for n, b in (("ctrl", 1), ("shift", 2), ("alt", 4), ("win", 8)) if mods & b]
    return "+".join(names + [KEY_NAMES.get(k, f"0x{k:02x}") for k in keys])


def build_key_macro(mods, keys):
    """Full slot image (from slot offset 0) for a keystroke, exactly as the driver builds it."""
    ev = [(0x80, b) for b in _MOD_DOWN_ORDER if mods & b]
    ev += [(0x81, k) for k in keys]
    ev += [(0x41, k) for k in reversed(keys)]
    ev += [(0x40, b) for b in _MOD_UP_ORDER if mods & b]
    body = bytes([len(ev)]) + b"".join(bytes([op, d, 0, 0x0F]) for op, d in ev)
    body += bytes([checksum(body)])
    return bytes(MACRO_HDR) + bytes(KEY_MACRO_MARK) + body


def decode_key_macro(raw):
    """raw = slot bytes starting at the marker (offset 0x1d). -> (mods, keys) or None."""
    if len(raw) < 4 or tuple(raw[:2]) != KEY_MACRO_MARK:
        return None
    n = raw[2]
    end = 3 + 4 * n
    if len(raw) < end + 1 or checksum(raw[2:end]) != raw[end]:
        return None
    mods, keys = 0, []
    for i in range(n):
        op, d = raw[3 + 4 * i], raw[4 + 4 * i]
        if op == 0x80:
            mods |= d
        elif op == 0x81:
            keys.append(d)
    return mods, keys


# --- transport --------------------------------------------------------------

def _hidiocsfeature(n):
    # _IOC(_IOC_WRITE | _IOC_READ, 'H', 0x06, n)
    return (3 << 30) | (n << 16) | (ord("H") << 8) | 0x06


def find_nodes():
    want = f"HID_ID=0003:{VID:08X}:{PID:08X}"
    nodes = []
    for d in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(d + "/device/uevent") as f:
                if want in f.read().upper():
                    nodes.append("/dev/" + os.path.basename(d))
        except OSError:
            continue
    return nodes


class HidrawTransport:
    def __init__(self, path=None):
        nodes = [path] if path else find_nodes()
        if not nodes:
            raise ZeloError("mouse not found (25a7:fa68)")
        self._fds = []
        self._tx = None
        denied = False
        for n in nodes:
            try:
                self._fds.append(os.open(n, os.O_RDWR | os.O_NONBLOCK))
            except PermissionError:
                denied = True
            except OSError:
                pass
        if not self._fds:
            raise ZeloError(
                "permission denied on /dev/hidraw* (enable programs.zelo or add the udev rule)"
                if denied else "cannot open the mouse's hidraw nodes")

    def send_feature(self, pkt):
        first = [self._tx] if self._tx is not None else []
        last = None
        for fd in first + [f for f in self._fds if f != self._tx]:
            try:
                fcntl.ioctl(fd, _hidiocsfeature(len(pkt)), bytearray(pkt))
                self._tx = fd
                return
            except OSError as e:  # interface without this feature report
                last = e
        raise ZeloError(f"no hidraw interface accepted the feature report ({last})")

    def drain(self):
        for fd in self._fds:
            while True:
                try:
                    if not os.read(fd, 64):
                        break
                except OSError:
                    break

    def wait(self, match, timeout):
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return None
            ready, _, _ = select.select(self._fds, [], [], left)
            for fd in ready:
                try:
                    rep = os.read(fd, 64)
                except OSError:
                    continue
                res = match(rep)
                if res is not None:
                    return res

    def close(self):
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds = []


# --- device -----------------------------------------------------------------

class Mouse:
    """Memory-level access. `transport` is injectable for tests."""

    def __init__(self, transport=None, path=None):
        self.t = transport if transport is not None else HidrawTransport(path)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.t.close()

    def read(self, addr, length, retries=3, timeout=0.3):
        out = bytearray()
        while len(out) < length:
            n = min(CHUNK, length - len(out))
            a = addr + len(out)
            req = build_request(CMD_READ, a, length=n)
            match = _matcher(CMD_READ, a, n)
            for _ in range(retries):
                self.t.drain()
                self.t.send_feature(req)
                data = self.t.wait(match, timeout)
                if data is not None:
                    out += data
                    break
            else:
                raise ZeloError(f"no answer reading 0x{a:04x}")
            time.sleep(0.005)
        return bytes(out)

    def write(self, addr, data, ack_timeout=0.3, wait=0.0):
        """Write `data` at `addr`. Returns True if every chunk was acknowledged.

        A wireless mouse that is asleep does not answer; with `wait` > 0 each chunk
        is re-sent (writes are idempotent) until it is acknowledged or `wait`
        seconds have passed. An unacknowledged write is not necessarily lost: the
        dongle may deliver it when the mouse wakes up."""
        acked = True
        for off in range(0, len(data), CHUNK):
            chunk = bytes(data[off:off + CHUNK])
            a = addr + off
            req = build_request(CMD_WRITE, a, chunk)
            match = _matcher(CMD_WRITE, a, len(chunk), need_data=False)
            deadline = time.monotonic() + wait
            while True:
                self.t.drain()
                self.t.send_feature(req)
                got = self.t.wait(match, ack_timeout)
                if got is not None or time.monotonic() >= deadline:
                    break
            acked = acked and got is not None
            time.sleep(0.005)
        return acked

    def read_led(self):
        return decode_led(self.read(ADDR_LED, LED_LEN))

    def write_led(self, mode, rgb, speed, light):
        return self.write(ADDR_LED, encode_led(mode, rgb, speed, light))

    # --- typed helpers -----------------------------------------------------

    def read_config(self):
        return self.read(0, CONFIG_LEN)

    def write_rate_byte(self, byte, **kw):
        return self.write(ADDR_RATE, encode_pair(byte), **kw)

    def write_stages(self, n, **kw):
        return self.write(ADDR_STAGES, encode_pair(n), **kw)

    def write_level(self, n, **kw):
        return self.write(ADDR_LEVEL, encode_pair(n), **kw)

    def write_dpi(self, slot, dpi, **kw):
        return self.write(ADDR_DPI + 3 * slot, encode_dpi(dpi), **kw)

    def write_key(self, slot, kind, code, param=0, **kw):
        return self.write(ADDR_KEYS + 4 * slot, encode_key(kind, code, param), **kw)

    def read_macro_header(self, slot):
        """Bytes from the marker on: header (3) + events + checksum, or b'' if empty."""
        base = ADDR_MACRO + slot * MACRO_SIZE + MACRO_HDR
        hdr = self.read(base, 3)
        if hdr[0] == 0:
            return b""
        n = min(hdr[2], 32)
        return hdr + self.read(base + 3, 4 * n + 1)

    def write_key_macro(self, slot, mods, keys, **kw):
        return self.write(ADDR_MACRO + slot * MACRO_SIZE, build_key_macro(mods, keys), **kw)
