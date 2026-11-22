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
    0x200+     macros, 0x180 bytes per slot
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

    def write(self, addr, data, ack_timeout=0.15):
        """Write `data` at `addr`. Returns True if every chunk was acknowledged
        (an unacknowledged write is not necessarily a failed one)."""
        acked = True
        for off in range(0, len(data), CHUNK):
            chunk = bytes(data[off:off + CHUNK])
            a = addr + off
            self.t.drain()
            self.t.send_feature(build_request(CMD_WRITE, a, chunk))
            got = self.t.wait(_matcher(CMD_WRITE, a, len(chunk), need_data=False), ack_timeout)
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

    def write_rate_byte(self, byte):
        return self.write(ADDR_RATE, encode_pair(byte))

    def write_stages(self, n):
        return self.write(ADDR_STAGES, encode_pair(n))

    def write_level(self, n):
        return self.write(ADDR_LEVEL, encode_pair(n))

    def write_dpi(self, slot, dpi):
        return self.write(ADDR_DPI + 3 * slot, encode_dpi(dpi))

    def write_key(self, slot, kind, code, param=0):
        return self.write(ADDR_KEYS + 4 * slot, encode_key(kind, code, param))
