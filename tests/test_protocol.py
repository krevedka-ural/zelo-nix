import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zelo import protocol as P  # noqa: E402


class FakeTransport:
    """Emulates the mouse memory; answers like the real device is believed to."""

    def __init__(self, with_id=True, answer=True, asleep_for=0):
        self.asleep_for = asleep_for  # first N feature reports get no answer and no effect
        self.mem = bytearray(0x1000)
        self.q = []
        self.with_id = with_id
        self.answer = answer
        self.sent = []

    def send_feature(self, pkt):
        assert len(pkt) == 15 and pkt[0] == 8
        assert pkt[14] == P.checksum(pkt[:14]), "bad report checksum"
        self.sent.append(bytes(pkt))
        cmd, a, n = pkt[1], pkt[3] << 8 | pkt[4], pkt[5]
        assert n <= 8
        if self.asleep_for > 0:
            self.asleep_for -= 1
            return
        if cmd == P.CMD_WRITE:
            self.mem[a:a + n] = pkt[6:6 + n]
            body = list(pkt[6:6 + n])
        elif cmd == P.CMD_READ:
            body = list(self.mem[a:a + n])
        else:
            raise AssertionError("unknown cmd")
        if not self.answer:
            return
        msg = [cmd, 0, pkt[3], pkt[4], n] + body
        msg += [0] * (16 - len(msg))
        self.q.append(bytes(([8] if self.with_id else []) + msg))

    def drain(self):
        self.q.clear()

    def wait(self, match, timeout):
        for rep in self.q:
            r = match(rep)
            if r is not None:
                return r
        return None

    def close(self):
        pass


class Tests(unittest.TestCase):
    def test_known_led_packet(self):
        raw = P.encode_led(1, [255, 0, 0], 2, 2)
        pkt = P.build_request(P.CMD_WRITE, P.ADDR_LED, raw)
        self.assertEqual(pkt.hex(" "), "08 03 00 00 35 07 01 ff 00 00 02 02 51 00 b9")

    def test_read_request(self):
        pkt = P.build_request(P.CMD_READ, 0x35, length=7)
        self.assertEqual(pkt.hex(" "), "08 04 00 00 35 07 00 00 00 00 00 00 00 00 " + f"{P.checksum(pkt[:14]):02x}")

    def test_ioctl_number(self):
        self.assertEqual(P._hidiocsfeature(9), 0xC0094806)
        self.assertEqual(P._hidiocsfeature(15), 0xC00F4806)

    def test_led_roundtrip_both_layouts(self):
        for with_id in (True, False):
            with P.Mouse(FakeTransport(with_id=with_id)) as m:
                self.assertTrue(m.write_led(3, [1, 2, 3], 4, 5))
                self.assertEqual(m.read_led(), {"mode": 3, "color": [1, 2, 3], "speed": 4, "light": 5})

    def test_chunking(self):
        t = FakeTransport()
        with P.Mouse(t) as m:
            data = bytes(range(1, 21))
            m.write(0x10, data)
            self.assertEqual(m.read(0x10, 20), data)
        self.assertEqual(bytes(t.mem[0x10:0x24]), data)

    def test_no_answer(self):
        with P.Mouse(FakeTransport(answer=False)) as m:
            with self.assertRaises(P.ZeloError):
                m.read(0, 4, retries=2, timeout=0.01)
            self.assertFalse(m.write(0x10, b"\x01", ack_timeout=0.01))

    def test_decode_led_rejects_bad_checksum(self):
        self.assertIsNone(P.decode_led(bytes([1, 2, 3, 4, 5, 6, 7])))


def factory_image():
    """A plausible 0x3c config image: 250 Hz (2.4G) / 125 Hz (BT), 5 stages, level 2."""
    img = bytearray(P.CONFIG_LEN)
    img[0:2] = P.encode_pair(P.encode_rate(250, 125))
    img[2:4] = P.encode_pair(5)
    img[4:6] = P.encode_pair(2)
    for i, v in enumerate([800, 1200, 1600, 2400, 2400]):
        img[6 + 3 * i:9 + 3 * i] = P.encode_dpi(v)
    # slots as read from a real F-36: middle, fn, left, right, fn, back, fn, forward
    keys = [(1, 0x04), (2, 0x01), (1, 0x01), (1, 0x02), (5, 0x00), (1, 0x08), (2, 0x02), (1, 0x10)]
    for i, (t, c) in enumerate(keys):
        img[0x15 + 4 * i:0x19 + 4 * i] = P.encode_key(t, c)
    img[0x35:0x3C] = P.encode_led(1, [255, 0, 0], 2, 2)
    return bytes(img)


class SleepTests(unittest.TestCase):
    def test_no_wait_reports_unacked(self):
        t = FakeTransport(asleep_for=1)
        with P.Mouse(t) as m:
            self.assertFalse(m.write(0x10, b"\x01", ack_timeout=0.01))
        self.assertEqual(t.mem[0x10], 0)

    def test_wait_retries_until_awake(self):
        t = FakeTransport(asleep_for=3)
        with P.Mouse(t) as m:
            self.assertTrue(m.write(0x10, b"\x07", ack_timeout=0.01, wait=2.0))
        self.assertEqual(t.mem[0x10], 7)
        self.assertEqual(len(t.sent), 4)  # 3 lost + 1 delivered

    def test_wait_gives_up(self):
        t = FakeTransport(asleep_for=10 ** 6)
        with P.Mouse(t) as m:
            self.assertFalse(m.write(0x10, b"\x07", ack_timeout=0.01, wait=0.1))


class MoreProtocolTests(unittest.TestCase):
    def test_rate_codes(self):
        self.assertEqual(P.encode_rate(250, 125), 0x84)
        self.assertEqual(P.decode_rate(0x84), (250, 125))
        # leaving a nibble as None keeps it
        self.assertEqual(P.encode_rate(500, None, 0x84), 0x82)
        self.assertEqual(P.encode_rate(None, 250, 0x82), 0x42)

    def test_dpi_table(self):
        self.assertEqual(P.encode_dpi(800), bytes([0x15, 0x15, P.checksum([0x15, 0x15])]))
        self.assertEqual(len(P.SENSOR_DPI), 21)
        with self.assertRaises(ValueError):
            P.encode_dpi(850)

    def test_decode_config(self):
        c = P.decode_config(factory_image())
        self.assertEqual(P.decode_rate(c["rate_byte"]), (250, 125))
        self.assertEqual((c["stages"], c["level"]), (5, 2))
        self.assertEqual([d["dpi"] for d in c["dpi"]], [800, 1200, 1600, 2400, 2400])
        self.assertTrue(all(k["ok"] for k in c["keys"]))
        self.assertEqual((c["keys"][5]["type"], c["keys"][5]["code"]), (1, 0x08))
        self.assertEqual(c["led"]["color"], [255, 0, 0])
        self.assertTrue(P.config_is_sane(factory_image()))

    def test_sanity_detects_corruption(self):
        bad = bytearray(factory_image())
        bad[0x07] ^= 0xFF
        self.assertFalse(P.config_is_sane(bytes(bad)))


class CliTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from zelo import cli
        self.cli = cli
        self.tmp = tempfile.mkdtemp()
        cli.STATE_PATH = os.path.join(self.tmp, "state.json")
        cli.BACKUP_DIR = os.path.join(self.tmp, "backups")
        self.fake = FakeTransport()
        self.fake.mem[0:P.CONFIG_LEN] = factory_image()
        cli.open_mouse = lambda path: P.Mouse(self.fake)
        self._real_sleep = None

    def run_cli(self, *argv):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.cli.main(list(argv))
        return buf.getvalue()

    def cfg(self):
        return P.decode_config(bytes(self.fake.mem[:P.CONFIG_LEN]))

    def test_rate_show_and_set(self):
        self.assertIn("2.4G 250 Hz, BT 125 Hz", self.run_cli("rate"))
        self.run_cli("rate", "500")
        self.assertEqual(P.decode_rate(self.cfg()["rate_byte"]), (500, 125))
        self.run_cli("rate", "--bt", "250")
        self.assertEqual(P.decode_rate(self.cfg()["rate_byte"]), (500, 250))
        with self.assertRaises(SystemExit):
            self.run_cli("rate", "1000")

    def test_dpi_set_and_backup(self):
        out = self.run_cli("dpi", "400", "800", "1600")
        self.assertIn("3 enabled", out)
        c = self.cfg()
        self.assertEqual([d["dpi"] for d in c["dpi"][:3]], [400, 800, 1600])
        self.assertEqual(c["stages"], 3)
        self.assertEqual(c["level"], 2)  # still valid: 0 <= 2 < 3
        files = os.listdir(self.cli.BACKUP_DIR)
        self.assertEqual(len(files), 1)
        with open(os.path.join(self.cli.BACKUP_DIR, files[0]), "rb") as f:
            self.assertEqual(f.read(), factory_image())  # backup = state before the write

    def test_dpi_stage_validation(self):
        with self.assertRaises(SystemExit):
            self.run_cli("dpi", "--stage", "9")
        with self.assertRaises(SystemExit):
            self.run_cli("dpi", "850")
        self.run_cli("dpi", "--stage", "1")
        self.assertEqual(self.cfg()["level"], 0)

    def test_restore_roundtrip(self):
        import tempfile
        self.run_cli("dpi", "400", "400", "400", "400", "400")
        path = os.path.join(self.tmp, "orig.bin")
        with open(path, "wb") as f:
            f.write(factory_image())
        out = self.run_cli("restore", path)  # no --yes: dry
        self.assertIn("--yes", out)
        self.assertEqual(self.cfg()["dpi"][0]["dpi"], 400)
        self.run_cli("restore", path, "--yes")
        self.assertEqual(bytes(self.fake.mem[:P.CONFIG_LEN]), factory_image())

    def test_restore_rejects_garbage(self):
        path = os.path.join(self.tmp, "bad.bin")
        with open(path, "wb") as f:
            f.write(b"\x00" * P.CONFIG_LEN)
        with self.assertRaises(SystemExit):
            self.run_cli("restore", path, "--yes")

    def test_info_and_keys_read_only(self):
        before = bytes(self.fake.mem)
        out = self.run_cli("info")
        self.assertIn("stage 3: 1600 DPI", out)
        self.assertIn("slot 5: 01 08 00  mouse back", out)
        self.run_cli("keys")
        self.assertEqual(bytes(self.fake.mem), before)

    def test_wait_flag_with_sleeping_mouse(self):
        self.fake.asleep_for = 2
        P_write = P.Mouse.write
        P.Mouse.write = lambda self, addr, data, ack_timeout=0.01, wait=0.0: P_write(self, addr, data, ack_timeout, wait)
        try:
            self.run_cli("-w", "2", "-c", "green")
        finally:
            P.Mouse.write = P_write
        self.assertEqual(self.cfg()["led"]["color"], [0, 255, 0])

    def test_keys_names(self):
        lines = self.run_cli("keys").splitlines()  # numbered like the vendor picture
        self.assertEqual(len(lines), 8)
        self.assertIn("mouse left", lines[0])
        self.assertIn("mouse right", lines[1])
        self.assertIn("mouse forward", lines[3])
        self.assertIn("dpi-loop", lines[5])
        self.assertIn("rgb", lines[6])
        self.assertIn("BT / 2.4G switch (fixed)", lines[7])

    def test_led_uses_mouse_state(self):
        self.run_cli("-c", "blue")
        led = self.cfg()["led"]
        self.assertEqual((led["mode"], led["color"]), (1, [0, 0, 255]))


class KeyMacroTests(unittest.TestCase):
    def test_all_named_keys_are_driver_supported(self):
        self.assertTrue(all(u in P.DRIVER_USAGES for u in P.KEYS.values()))

    def test_parse_and_format(self):
        mods, keys = P.parse_keystroke("Ctrl+Shift+T")
        self.assertEqual((mods, keys), (3, [P.KEYS["t"]]))
        self.assertEqual(P.format_keystroke(mods, keys), "ctrl+shift+t")
        for bad in ("", "ctrl+", "foo", "a+b+c+d"):
            with self.assertRaises(ValueError):
                P.parse_keystroke(bad)

    def test_macro_layout_matches_driver(self):
        img = P.build_key_macro(*P.parse_keystroke("ctrl+shift+a"))
        self.assertEqual(img[:0x1D], bytes(0x1D))
        self.assertEqual(img[0x1D:0x1F], b"\x52\x03")
        self.assertEqual(img[0x1F], 6)  # 2 mod down, key down, key up, 2 mod up
        ev = [tuple(img[0x20 + 4 * i:0x24 + 4 * i]) for i in range(6)]
        self.assertEqual(ev, [(0x80, 1, 0, 15), (0x80, 2, 0, 15), (0x81, 4, 0, 15),
                              (0x41, 4, 0, 15), (0x40, 2, 0, 15), (0x40, 1, 0, 15)])
        self.assertEqual(img[-1], P.checksum(img[0x1F:-1]))
        self.assertEqual(len(img), 0x1F + 1 + 4 * 6 + 1)

    def test_single_key_length_matches_driver(self):
        # driver: total = 4*n + 0x21 for n events
        self.assertEqual(len(P.build_key_macro(0, [P.KEYS["a"]])), 4 * 2 + 0x21)

    def test_decode_roundtrip_and_corruption(self):
        img = P.build_key_macro(*P.parse_keystroke("alt+f4"))
        self.assertEqual(P.decode_key_macro(img[0x1D:]), (4, [P.KEYS["f4"]]))
        bad = bytearray(img)
        bad[0x22] ^= 1
        self.assertIsNone(P.decode_key_macro(bytes(bad[0x1D:])))
        self.assertIsNone(P.decode_key_macro(bytes(8)))


class BindTests(CliTests):
    def entry(self, slot):
        return self.cfg()["keys"][slot]

    def test_bind_function_and_button(self):
        out = self.run_cli("bind", "6", "dpi-")          # button 6 = DPI button = slot 1
        self.assertIn("verified: yes", out)
        e = self.entry(1)
        self.assertEqual((e["type"], e["code"], e["ok"]), (2, 3, True))
        self.run_cli("bind", "3", "back")                 # button 3 = wheel = slot 0
        self.assertEqual((self.entry(0)["type"], self.entry(0)["code"]), (1, 0x08))
        self.run_cli("bind", "7", "none")                 # button 7 = LED strip button = slot 4
        self.assertEqual((self.entry(4)["type"], self.entry(4)["code"]), (0, 0))

    def test_bind_keystroke(self):
        out = self.run_cli("bind", "2", "key:ctrl+shift+t")   # button 2 = right = slot 3
        self.assertIn("verified: yes", out)
        e = self.entry(3)
        self.assertEqual((e["type"], e["code"], e["param"], e["ok"]), (4, 3, 1, True))
        base = 0x200 + 3 * 0x180
        self.assertEqual(bytes(self.fake.mem[base + 0x1D:base + 0x1F]), b"\x52\x03")
        self.assertIn("key:ctrl+shift+t", self.run_cli("keys").splitlines()[1])

    def test_rebind_over_known_macro_is_fine_unknown_needs_force(self):
        self.run_cli("bind", "2", "key:a")
        self.run_cli("bind", "2", "key:b")  # decodable -> ok
        self.assertIn("key:b", self.run_cli("keys").splitlines()[1])
        base = 0x200 + 3 * 0x180
        self.fake.mem[base + 0x20] ^= 0xFF  # corrupt -> unknown format
        with self.assertRaises(SystemExit):
            self.run_cli("bind", "2", "left")
        self.run_cli("bind", "2", "left", "--force")
        self.assertEqual(self.entry(3)["type"], 1)

    def test_bad_input(self):
        for args in (("bind", "9", "left"), ("bind", "0", "left"), ("bind", "1", "fly"),
                     ("bind", "1", "key:ctrl+nope")):
            with self.assertRaises(SystemExit):
                self.run_cli(*args)

    def test_bt_switch_button_is_protected(self):
        before = bytes(self.fake.mem)
        with self.assertRaises(SystemExit):
            self.run_cli("bind", "8", "left")
        with self.assertRaises(SystemExit):
            self.run_cli("bind", "6", "left", "--slot")   # raw slot 6 is the same switch
        self.assertEqual(bytes(self.fake.mem), before)

    def test_raw_slot_flag(self):
        self.run_cli("bind", "4", "dpi+", "--slot")       # raw slot 4, not button 4
        e = self.entry(4)
        self.assertEqual((e["type"], e["code"]), (2, 2))

    def test_backup_made_before_bind(self):
        self.run_cli("bind", "6", "dpi-")
        files = os.listdir(self.cli.BACKUP_DIR)
        with open(os.path.join(self.cli.BACKUP_DIR, files[0]), "rb") as f:
            self.assertEqual(f.read(), factory_image())


if __name__ == "__main__":
    unittest.main()
