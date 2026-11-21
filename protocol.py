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

Known memory map (from driver code; see zelo.cli `info`):
    0x00..0x34  main config; DPI stages @0x06 (5 x 3 B), DPI colors @0x15 (8 x 4 B)
    0x35..0x3b  LED block: mode, R, G, B, speed, light, checksum
    0x200+      macros, 0x180 bytes per slot

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
