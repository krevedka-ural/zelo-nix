import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from test_protocol import FakeTransport, factory_image  # noqa: E402
from zelo import api, cli  # noqa: E402
from zelo import protocol as P  # noqa: E402


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cli.STATE_PATH = os.path.join(self.tmp, "state.json")
        cli.BACKUP_DIR = os.path.join(self.tmp, "backups")
        self.fake = FakeTransport()
        self.fake.mem[0:P.CONFIG_LEN] = factory_image()
        self.m = P.Mouse(self.fake)

    def test_snapshot(self):
        s = api.snapshot(self.m)
        acts = [x["action"] for x in s["slots"]]  # memory order; slot 6 is the BT/2.4G switch
        self.assertEqual(acts, ["middle", "dpi-loop", "left", "right", "rgb", "back", "bt-switch", "forward"])
        by_button = [x["action"] for x in s["buttons"]]  # vendor picture order 1..8
        self.assertEqual(by_button, ["left", "right", "middle", "forward", "back", "dpi-loop", "rgb", "bt-switch"])
        self.assertEqual([x["button"] for x in s["buttons"]], list(range(1, 9)))
        self.assertEqual([x["locked"] for x in s["buttons"]], [False] * 7 + [True])
        self.assertEqual(s["cfg"]["stages"], 5)

    def test_snapshot_reads_key_macro(self):
        api.bind(self.m, 3, "key:ctrl+shift+t")
        self.assertEqual(api.snapshot(self.m)["slots"][3]["action"], "key:ctrl+shift+t")

    def test_set_led(self):
        r = api.set_led(self.m, 2, [10, 20, 30], 3, 4)
        self.assertTrue(r["acked"] and r["verified"])
        self.assertEqual(self.m.read_led()["color"], [10, 20, 30])
        with self.assertRaises(ValueError):
            api.set_led(self.m, 2, [300, 0, 0], 3, 4)

    def test_set_rate_changed_flag_and_backup(self):
        r = api.set_rate(self.m, 500, None)
        self.assertTrue(r["changed"] and r["verified"] and r["backup"])
        self.assertEqual(P.decode_rate(P.decode_config(self.m.read_config())["rate_byte"]), (500, 125))
        r2 = api.set_rate(self.m, 500, None)
        self.assertFalse(r2["changed"])
        with self.assertRaises(ValueError):
            api.set_rate(self.m, 1000, None)

    def test_set_dpi(self):
        r = api.set_dpi(self.m, [400, 800, 1200, None, 2400], 4, 3)
        self.assertTrue(r["verified"])
        c = P.decode_config(self.m.read_config())
        self.assertEqual([d["dpi"] for d in c["dpi"]], [400, 800, 1200, 2400, 2400])  # slot 3 untouched
        self.assertEqual((c["stages"], c["level"]), (4, 3))
        self.assertFalse(api.set_dpi(self.m, [400, 800, 1200, None, 2400], 4, 3)["changed"])
        for bad in (([400] * 4, 3, 0), ([850] * 5, 3, 0), ([400] * 5, 3, 3), ([400] * 5, 0, 0)):
            with self.assertRaises(ValueError):
                api.set_dpi(self.m, *bad)

    def test_bind(self):
        r = api.bind(self.m, 1, "dpi-")
        self.assertTrue(r["verified"])
        self.assertEqual(api.snapshot(self.m)["slots"][1]["action"], "dpi-")
        with self.assertRaises(ValueError):
            api.bind(self.m, 8, "left")
        with self.assertRaises(ValueError):
            api.bind(self.m, 1, "key:ctrl+nope")

    def test_bt_switch_is_protected(self):
        with self.assertRaises(P.ZeloError):
            api.bind(self.m, 6, "left")
        with self.assertRaises(P.ZeloError):
            api.bind_button(self.m, 8, "left")
        self.assertEqual(self.m.read(0x15 + 4 * 6, 3), bytes([2, 2, 0]))  # untouched

    def test_bind_button_uses_picture_numbers(self):
        self.assertTrue(api.bind_button(self.m, 6, "dpi-")["verified"])      # button 6 = DPI button = slot 1
        self.assertEqual(api.snapshot(self.m)["buttons"][5]["action"], "dpi-")
        with self.assertRaises(ValueError):
            api.bind_button(self.m, 9, "left")

    def test_summarize(self):
        ok = {"acked": True, "verified": True, "changed": True, "backup": None}
        self.assertEqual(api.summarize([ok]), "Применено ✓")
        self.assertEqual(api.summarize([{**ok, "changed": False}]), "Изменений нет")
        self.assertIn("спит", api.summarize([{**ok, "acked": False, "verified": False}]))
        self.assertIn("отличается", api.summarize([{**ok, "verified": False}]))

    def test_sleeping_mouse_unverified(self):
        self.fake.asleep_for = 10 ** 6
        r = api.set_led(self.m, 1, [1, 2, 3], 1, 1)
        self.assertFalse(r["acked"])
        self.assertFalse(r["verified"])


if __name__ == "__main__":
    unittest.main()
