"""GUI logic test: runs against real PySide6 (offscreen) when installed, else is skipped.

Device operations are executed synchronously against the fake mouse from
test_protocol, so this covers: loading a snapshot into the widgets, collecting
values from them, and the writes that result.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication  # noqa: F401
    HAVE_QT = True
except ImportError:
    HAVE_QT = False

from test_protocol import FakeTransport, factory_image  # noqa: E402
from zelo import cli  # noqa: E402
from zelo import protocol as P  # noqa: E402


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from zelo import gui
        self.gui = gui
        tmp = tempfile.mkdtemp()
        cli.STATE_PATH = os.path.join(tmp, "state.json")
        cli.BACKUP_DIR = os.path.join(tmp, "backups")
        self.fake = FakeTransport()
        self.fake.mem[0:P.CONFIG_LEN] = factory_image()
        fake = self.fake

        def run_sync(win, fn, on_done=None, busy=None):
            res = fn(P.Mouse(fake))
            if on_done:
                on_done(res)

        gui.MainWindow.run_device = run_sync
        self.win = gui.MainWindow()
        self.win.refresh()

    def cfg(self):
        return P.decode_config(bytes(self.fake.mem[:P.CONFIG_LEN]))

    def test_buttons_loaded(self):
        f = self.win.buttons_tab.func  # button number -> FunctionButton
        self.assertEqual(f[1].btn.text(), "Левая кнопка")
        self.assertEqual(f[3].btn.text(), "Средняя кнопка")
        self.assertEqual(f[6].btn.text(), "DPI: по кругу")
        self.assertEqual(f[8].btn.text(), "Переключение BT / 2.4G")
        self.assertFalse(f[8].btn.isEnabled())
        self.assertTrue(f[1].btn.isEnabled())
        self.assertEqual(f[1].caption.text(), "1: ЛКМ")
        self.assertEqual(sorted(f), list(range(1, 9)))

    def test_light_apply(self):
        t = self.win.light_tab
        t.mode.setCurrentIndex(t.mode.findData(cli.MODES["rainbow"]))
        t.set_color([1, 2, 3])
        t.speed.setValue(3)
        t.light.setValue(4)
        t.apply()
        led = self.cfg()["led"]
        self.assertEqual((led["mode"], led["color"], led["speed"], led["light"]), (2, [1, 2, 3], 3, 4))

    def test_sensor_apply(self):
        t = self.win.sensor_tab
        t.count.setValue(3)
        t.radios[1].setChecked(True)
        t.combos[0].setCurrentIndex(t.combos[0].findData(400))
        t.rate_24.setCurrentIndex(t.rate_24.findData(500))
        t.apply()
        c = self.cfg()
        self.assertEqual((c["stages"], c["level"], c["dpi"][0]["dpi"]), (3, 1, 400))
        self.assertEqual(P.decode_rate(c["rate_byte"])[0], 500)

    def test_bind_through_dialog(self):
        gui = self.gui

        class FakeDialog:
            def __init__(self, parent, button, title, current):
                self.args = (button, title, current)

            def exec(self):
                from PySide6.QtWidgets import QDialog
                return QDialog.DialogCode.Accepted

            def action(self):
                return "key:ctrl+shift+t"

        gui.ActionDialog = FakeDialog
        self.win.buttons_tab.edit(2)          # button 2 = right button = memory slot 3
        e = self.cfg()["keys"][3]
        self.assertEqual((e["type"], e["code"]), (4, 3))
        self.assertEqual(self.win.buttons_tab.func[2].btn.text(), "Клавиши: ctrl+shift+t")


if __name__ == "__main__":
    unittest.main()
