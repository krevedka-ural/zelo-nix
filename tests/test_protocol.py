import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zelo import protocol as P  # noqa: E402


class FakeTransport:
    """Emulates the mouse memory; answers like the real device is believed to."""

    def __init__(self, with_id=True, answer=True):
        self.mem = bytearray(0x400)
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


if __name__ == "__main__":
    unittest.main()
