"""zelo-gui - Qt interface for the Zelotes F-36 (PySide6, no .ui files).

Layout of this file, top to bottom:
  1. background execution of device operations (never blocks the window)
  2. small widgets (key capture, action dialog)
  3. the three tabs: Buttons / Sensor / Lighting
  4. the main window and `main()`

All device logic lives in zelo/api.py; this file only shows data and collects input.
Every tab has `load(snapshot)` (fill widgets from the mouse) and an apply button.
"""
import itertools
import sys
import threading

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QRadioButton, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

from . import api, cli
from . import protocol as P

WAIT_SECONDS = 8.0  # how long to keep re-sending to a sleeping mouse

# ---------------------------------------------------------------------------
# texts (all UI strings in one place, easy to edit by hand)
# ---------------------------------------------------------------------------

ACTIONS = [  # (label, action string understood by api.bind)
    ("Левая кнопка", "left"),
    ("Правая кнопка", "right"),
    ("Средняя кнопка", "middle"),
    ("Назад", "back"),
    ("Вперёд", "forward"),
    ("DPI: по кругу", "dpi-loop"),
    ("DPI +", "dpi+"),
    ("DPI −", "dpi-"),
    ("Переключатель подсветки", "rgb"),
    ("Ничего не делать", "none"),
    ("Сочетание клавиш…", "key"),
]
ACTION_LABEL = {act: label for label, act in ACTIONS}

MODE_CHOICES = [  # (label, zelo mode name); order of modes is inferred from the driver
    ("Статичный", "static"),
    ("Радуга", "rainbow"),
    ("Неон", "rainbow_clippy"),
    ("Дыхание", "breathing"),
    ("Выключена", "off"),
]

COLOR_PRESETS = [(n, c) for n, c in cli.COLORS.items() if n != "yelow"]


def action_text(action, raw):
    if action is None:
        return f"неизвестно ({raw[0]:02x} {raw[1]:02x})"
    if action.startswith("key:"):
        return "Клавиши: " + action[4:]
    return ACTION_LABEL.get(action, action)


# ---------------------------------------------------------------------------
# 1. background execution
# ---------------------------------------------------------------------------

_device_lock = threading.Lock()


class _Signals(QObject):
    done = Signal(int, object)
    failed = Signal(int, str)


class Task(QRunnable):
    def __init__(self, tid, fn):
        super().__init__()
        self.setAutoDelete(False)  # Python owns the runnable (we keep it in MainWindow._tasks)
        self.tid = tid
        self.fn = fn
        self.signals = _Signals()

    def run(self):
        try:
            result = self.fn()
        except SystemExit as e:  # cli helpers may call sys.exit(msg)
            self.signals.failed.emit(self.tid, str(e.code))
        except Exception as e:  # noqa: BLE001 - show anything to the user
            self.signals.failed.emit(self.tid, str(e))
        else:
            self.signals.done.emit(self.tid, result)


# ---------------------------------------------------------------------------
# 2. small widgets
# ---------------------------------------------------------------------------

def _build_qt_keys():
    """Qt key code -> zelo key name."""
    table = {}

    def add(qt_name, name):
        key = getattr(Qt.Key, qt_name, None)
        if key is not None:
            table[getattr(key, "value", key)] = name

    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        add(f"Key_{c}", c.lower())
    for c in "0123456789":
        add(f"Key_{c}", c)
    for i in range(1, 13):
        add(f"Key_F{i}", f"f{i}")
    named = {
        "Key_Return": "enter", "Key_Enter": "kpenter", "Key_Escape": "esc",
        "Key_Backspace": "backspace", "Key_Tab": "tab", "Key_Space": "space",
        "Key_Minus": "minus", "Key_Equal": "equal", "Key_BracketLeft": "lbracket",
        "Key_BracketRight": "rbracket", "Key_Backslash": "backslash",
        "Key_Semicolon": "semicolon", "Key_Apostrophe": "quote", "Key_QuoteLeft": "grave",
        "Key_Comma": "comma", "Key_Period": "period", "Key_Slash": "slash",
        "Key_CapsLock": "capslock", "Key_Print": "printscreen",
        "Key_ScrollLock": "scrolllock", "Key_Pause": "pause", "Key_Insert": "insert",
        "Key_Home": "home", "Key_PageUp": "pageup", "Key_Delete": "delete",
        "Key_End": "end", "Key_PageDown": "pagedown", "Key_Right": "arrow-right",
        "Key_Left": "arrow-left", "Key_Down": "arrow-down", "Key_Up": "arrow-up",
        "Key_NumLock": "numlock", "Key_Menu": "menu",
    }
    for q, n in named.items():
        add(q, n)
    return table


QT_KEYS = _build_qt_keys()


class KeyCapture(QLineEdit):
    """Press a shortcut while this field has focus; it records ctrl+shift+t style text."""

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setPlaceholderText("Нажмите нужное сочетание…")

    def keyPressEvent(self, ev):  # noqa: N802 (Qt naming)
        name = QT_KEYS.get(ev.key())
        if name is None:  # a lone modifier or a key zelo doesn't know
            return
        mods = ev.modifiers()
        parts = []
        if mods & Qt.KeyboardModifier.ControlModifier:
            parts.append("ctrl")
        if mods & Qt.KeyboardModifier.ShiftModifier:
            parts.append("shift")
        if mods & Qt.KeyboardModifier.AltModifier:
            parts.append("alt")
        if mods & Qt.KeyboardModifier.MetaModifier:
            parts.append("win")
        self.setText("+".join(parts + [name]))


class ActionDialog(QDialog):
    def __init__(self, parent, slot, current):
        super().__init__(parent)
        self.setWindowTitle(f"Слот {slot}")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Что должна делать эта кнопка?"))

        self.combo = QComboBox()
        for label, act in ACTIONS:
            self.combo.addItem(label, act)
        lay.addWidget(self.combo)

        self.keys = KeyCapture()
        lay.addWidget(self.keys)

        if current and current.startswith("key:"):
            self.combo.setCurrentIndex(self.combo.findData("key"))
            self.keys.setText(current[4:])
        elif current:
            self.combo.setCurrentIndex(max(0, self.combo.findData(current)))
        self.combo.currentIndexChanged.connect(self._sync)
        self._sync()

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._ok)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

    def _sync(self):
        is_key = self.combo.currentData() == "key"
        self.keys.setVisible(is_key)
        if is_key:
            self.keys.setFocus()

    def _ok(self):
        if self.combo.currentData() == "key":
            try:
                P.parse_keystroke(self.keys.text())
            except ValueError as e:
                QMessageBox.warning(self, "Сочетание клавиш", f"Не подходит: {e}")
                return
        self.accept()

    def action(self):
        data = self.combo.currentData()
        return "key:" + self.keys.text() if data == "key" else data


# ---------------------------------------------------------------------------
# 3. tabs
# ---------------------------------------------------------------------------

class ButtonsTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.slots = []
        outer = QVBoxLayout(self)
        outer.addWidget(QLabel(
            "Слоты — это кнопки мыши в порядке памяти; как они расположены на корпусе, "
            "лучше проверить опытом. «По умолчанию» — заводское назначение слота."))
        grid = QGridLayout()
        for col, title in enumerate(("Слот", "По умолчанию", "Сейчас", "")):
            lbl = QLabel(f"<b>{title}</b>")
            grid.addWidget(lbl, 0, col)
        self.now_labels = []
        for i in range(P.KEY_SLOTS):
            grid.addWidget(QLabel(str(i)), i + 1, 0)
            factory = ACTION_LABEL.get(P.FACTORY_SLOTS[i], P.FACTORY_SLOTS[i])
            grid.addWidget(QLabel(factory), i + 1, 1)
            now = QLabel("—")
            self.now_labels.append(now)
            grid.addWidget(now, i + 1, 2)
            btn = QPushButton("Изменить…")
            btn.clicked.connect(lambda _=False, n=i: self.edit(n))
            grid.addWidget(btn, i + 1, 3)
        grid.setColumnStretch(2, 1)
        outer.addLayout(grid)
        outer.addStretch(1)

    def load(self, snap):
        self.slots = snap["slots"]
        for s in self.slots:
            self.now_labels[s["slot"]].setText(action_text(s["action"], s["raw"]))

    def edit(self, slot):
        current = self.slots[slot]["action"] if self.slots else None
        dlg = ActionDialog(self, slot, current)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        action = dlg.action()
        wait = self.win.wait_seconds()
        self.win.run_device(lambda m: [api.bind(m, slot, action, wait=wait)],
                            self.win.after_write, busy=f"Записываю слот {slot}…")


class SensorTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        outer = QVBoxLayout(self)

        rate_box = QGroupBox("Частота опроса")
        rate_form = QFormLayout(rate_box)
        self.rate_24 = QComboBox()
        self.rate_bt = QComboBox()
        for combo in (self.rate_24, self.rate_bt):
            for hz in api.RATES:
                combo.addItem(f"{hz} Гц", hz)
        rate_form.addRow("Донгл 2.4G / провод:", self.rate_24)
        rate_form.addRow("Bluetooth:", self.rate_bt)
        outer.addWidget(rate_box)

        dpi_box = QGroupBox("DPI")
        dpi_lay = QVBoxLayout(dpi_box)
        top = QHBoxLayout()
        top.addWidget(QLabel("Число ступеней:"))
        self.count = QSpinBox()
        self.count.setRange(1, P.DPI_SLOTS)
        self.count.valueChanged.connect(self._sync_enabled)
        top.addWidget(self.count)
        top.addStretch(1)
        top.addWidget(QLabel("Выбранная ступень — кружок слева"))
        dpi_lay.addLayout(top)

        self.group = QButtonGroup(self)
        self.radios, self.combos = [], []
        for i in range(P.DPI_SLOTS):
            row = QHBoxLayout()
            radio = QRadioButton(f"Ступень {i + 1}")
            self.group.addButton(radio, i)
            combo = QComboBox()
            for v in api.DPI_VALUES:
                combo.addItem(f"{v} DPI", v)
            row.addWidget(radio)
            row.addWidget(combo, 1)
            dpi_lay.addLayout(row)
            self.radios.append(radio)
            self.combos.append(combo)
        outer.addWidget(dpi_box)

        self.apply_btn = QPushButton("Применить")
        self.apply_btn.clicked.connect(self.apply)
        outer.addWidget(self.apply_btn)
        outer.addStretch(1)

    def _sync_enabled(self):
        n = self.count.value()
        for i in range(P.DPI_SLOTS):
            self.radios[i].setEnabled(i < n)
            self.combos[i].setEnabled(i < n)

    def load(self, snap):
        cfg = snap["cfg"]
        g24, bt = P.decode_rate(cfg["rate_byte"]) if cfg["rate_byte"] is not None else (None, None)
        for combo, hz in ((self.rate_24, g24), (self.rate_bt, bt)):
            combo.setCurrentIndex(max(0, combo.findData(hz)))
        for i, d in enumerate(cfg["dpi"]):
            idx = self.combos[i].findData(d["dpi"]) if d["dpi"] else -1
            if idx >= 0:
                self.combos[i].setCurrentIndex(idx)
        if cfg["stages"] is not None:
            self.count.setValue(max(1, min(P.DPI_SLOTS, cfg["stages"])))
        level = cfg["level"] if cfg["level"] is not None else 0
        self.radios[max(0, min(P.DPI_SLOTS - 1, level))].setChecked(True)
        self._sync_enabled()

    def apply(self):
        g24, bt = self.rate_24.currentData(), self.rate_bt.currentData()
        values = [c.currentData() for c in self.combos]
        count = self.count.value()
        stage = self.group.checkedId()
        if not 0 <= stage < count:
            QMessageBox.warning(self, "DPI", "Выбранная ступень должна быть среди включённых.")
            return
        wait = self.win.wait_seconds()
        self.win.run_device(
            lambda m: [api.set_rate(m, g24, bt, wait=wait),
                       api.set_dpi(m, values, count, stage, wait=wait)],
            self.win.after_write, busy="Записываю настройки сенсора…")


class LightTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.rgb = [255, 255, 255]
        outer = QVBoxLayout(self)
        form = QFormLayout()

        self.mode = QComboBox()
        for label, name in MODE_CHOICES:
            self.mode.addItem(label, cli.MODES[name])
        form.addRow("Режим:", self.mode)

        row = QHBoxLayout()
        self.swatch = QPushButton()
        self.swatch.setFixedSize(90, 28)
        self.swatch.clicked.connect(self.pick_color)
        row.addWidget(self.swatch)
        for name, rgb in COLOR_PRESETS:
            b = QPushButton()
            b.setFixedSize(24, 24)
            b.setToolTip(name)
            b.setStyleSheet(f"background-color: rgb({rgb[0]},{rgb[1]},{rgb[2]}); border: 1px solid #666;")
            b.clicked.connect(lambda _=False, c=rgb: self.set_color(list(c)))
            row.addWidget(b)
        row.addStretch(1)
        form.addRow("Цвет:", row)

        self.speed = QSpinBox()
        self.speed.setRange(0, 10)
        self.speed.setToolTip("Диапазон скорости не проверен: в драйвере он не нашёлся")
        form.addRow("Скорость:", self.speed)
        self.light = QSpinBox()
        self.light.setRange(0, 10)
        self.light.setToolTip("Диапазон яркости не проверен: в драйвере он не нашёлся")
        form.addRow("Яркость:", self.light)
        outer.addLayout(form)

        self.apply_btn = QPushButton("Применить")
        self.apply_btn.clicked.connect(self.apply)
        outer.addWidget(self.apply_btn)
        outer.addStretch(1)
        self.set_color(self.rgb)

    def set_color(self, rgb):
        self.rgb = list(rgb)
        self.swatch.setStyleSheet(
            f"background-color: rgb({rgb[0]},{rgb[1]},{rgb[2]}); border: 1px solid #666;")

    def pick_color(self):
        c = QColorDialog.getColor(QColor(*self.rgb), self, "Цвет подсветки")
        if c.isValid():
            self.set_color([c.red(), c.green(), c.blue()])

    def load(self, snap):
        led = snap["cfg"]["led"]
        if not led:
            return
        idx = self.mode.findData(led["mode"])
        if idx < 0:
            self.mode.addItem(f"Режим {led['mode']}", led["mode"])
            idx = self.mode.count() - 1
        self.mode.setCurrentIndex(idx)
        self.set_color(led["color"])
        for spin, v in ((self.speed, led["speed"]), (self.light, led["light"])):
            spin.setMaximum(max(spin.maximum(), v))
            spin.setValue(v)

    def apply(self):
        mode, rgb = self.mode.currentData(), list(self.rgb)
        speed, light = self.speed.value(), self.light.value()
        wait = self.win.wait_seconds()
        self.win.run_device(lambda m: [api.set_led(m, mode, rgb, speed, light, wait=wait)],
                            self.win.after_write, busy="Записываю подсветку…")


# ---------------------------------------------------------------------------
# 4. main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Zelotes F-36")
        self.resize(680, 560)
        self.snapshot = None
        self._ids = itertools.count(1)
        self._tasks = {}      # tid -> Task (keeps the runnable alive)
        self._callbacks = {}  # tid -> on_done
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(1)  # device operations are strictly sequential

        central = QWidget()
        lay = QVBoxLayout(central)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("background: #7a2a2a; color: white; padding: 6px;")
        self.banner.hide()
        lay.addWidget(self.banner)

        self.tabs = QTabWidget()
        self.buttons_tab = ButtonsTab(self)
        self.sensor_tab = SensorTab(self)
        self.light_tab = LightTab(self)
        self.tabs.addTab(self.buttons_tab, "Кнопки")
        self.tabs.addTab(self.sensor_tab, "Сенсор")
        self.tabs.addTab(self.light_tab, "Подсветка")
        lay.addWidget(self.tabs)
        self.setCentralWidget(central)

        self.wait_box = QCheckBox("Ждать, пока мышь проснётся")
        self.wait_box.setChecked(True)
        self.wait_box.setToolTip(
            "Беспроводная мышь в покое спит и не отвечает; программа будет повторять "
            f"запись до {int(WAIT_SECONDS)} с. Можно просто подвигать мышью.")
        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.clicked.connect(self.refresh)
        bar = self.statusBar()
        bar.addPermanentWidget(self.wait_box)
        bar.addPermanentWidget(self.refresh_btn)

        self.tabs.setEnabled(False)
        QTimer.singleShot(0, self.refresh)

    # --- plumbing ----------------------------------------------------------

    def wait_seconds(self):
        return WAIT_SECONDS if self.wait_box.isChecked() else 0.0

    def run_device(self, fn, on_done=None, busy=None):
        """Run fn(mouse) in the background; on_done(result) runs in the GUI thread."""
        def job():
            with _device_lock:
                with P.Mouse() as mouse:
                    return fn(mouse)

        tid = next(self._ids)
        task = Task(tid, job)
        task.signals.done.connect(self._on_done)
        task.signals.failed.connect(self._on_failed)
        self._tasks[tid] = task
        self._callbacks[tid] = on_done
        if busy:
            self.statusBar().showMessage(busy)
        self.tabs.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.pool.start(task)

    def _finish(self, tid):
        self._tasks.pop(tid, None)
        cb = self._callbacks.pop(tid, None)
        self.tabs.setEnabled(self.snapshot is not None)
        self.refresh_btn.setEnabled(True)
        return cb

    @Slot(int, object)
    def _on_done(self, tid, result):
        cb = self._finish(tid)
        if cb:
            cb(result)

    @Slot(int, str)
    def _on_failed(self, tid, message):
        self._finish(tid)
        self.show_problem(message)

    def show_problem(self, message):
        hint = ""
        if "permission" in message.lower():
            hint = "\nВключи programs.zelo.enable = true (правило udev) и переподключи мышь."
        elif "not found" in message.lower():
            hint = "\nПроверь, что донгл вставлен (25a7:fa68)."
        self.banner.setText("Ошибка: " + message + hint)
        self.banner.show()
        self.statusBar().showMessage("Нет связи с мышью", 8000)

    # --- actions -----------------------------------------------------------

    def refresh(self):
        self.run_device(api.snapshot, self._loaded, busy="Читаю настройки мыши…")

    def _loaded(self, snap):
        self.snapshot = snap
        self.banner.hide()
        for tab in (self.buttons_tab, self.sensor_tab, self.light_tab):
            tab.load(snap)
        self.tabs.setEnabled(True)
        if self.statusBar().currentMessage().startswith("Читаю"):
            self.statusBar().showMessage("Подключено", 4000)

    def after_write(self, results):
        self.statusBar().showMessage(api.summarize(results), 10000)
        self.refresh_after_write()

    def refresh_after_write(self):
        # reload what the mouse now reports, but keep the message above visible
        self.run_device(api.snapshot, self._loaded)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
