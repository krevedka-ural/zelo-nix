"""Qt-free application layer: everything the GUI does goes through here.

Every function takes an open `protocol.Mouse`, raises `protocol.ZeloError` (device
problems) or `ValueError` (bad input), and returns plain data. Write functions
return {"acked", "verified", "changed", "backup"}:
  acked     the mouse acknowledged every packet
  verified  reading the memory back gives what we wrote
  changed   False if there was nothing to write
  backup    path of the config backup made before writing (or None)
"""
from . import cli
from . import protocol as P

RATES = (125, 250, 500)
DPI_VALUES = tuple(sorted(P.SENSOR_DPI))


# --- reading ----------------------------------------------------------------

def slot_action(mouse, k):
    """Action string for a matrix entry (same syntax as `zelo bind`), or None if unknown."""
    if not k["ok"]:
        return None
    if k["type"] == P.TYPE_BUTTON:
        return cli.BUTTON_BITS.get(k["code"])
    name = cli.FUNC_NAMES.get((k["type"], k["code"]))
    if name:
        return name
    if k["type"] == P.TYPE_MACRO:
        try:
            dec = P.decode_key_macro(mouse.read_macro_header(k["code"]))
        except P.ZeloError:
            dec = None
        if dec:
            return "key:" + P.format_keystroke(*dec)
    return None


def snapshot(mouse):
    cfg = P.decode_config(mouse.read_config())
    slots = []
    for i, k in enumerate(cfg["keys"]):
        slots.append({"slot": i, "action": slot_action(mouse, k), "ok": k["ok"],
                      "raw": (k["type"], k["code"], k["param"]),
                      "factory": P.FACTORY_SLOTS[i]})
    return {"cfg": cfg, "slots": slots}


# --- writing ----------------------------------------------------------------

def _check(fn):
    """Run a read-back check; an unanswered read just means 'not verified'."""
    try:
        return bool(fn())
    except P.ZeloError:
        return False


def _result(acked, verified, changed=True, backup=None):
    return {"acked": acked, "verified": verified, "changed": changed, "backup": backup}


def set_led(mouse, mode, rgb, speed, light, wait=0.0):
    rgb = [int(x) for x in rgb]
    for v in (mode, speed, light, *rgb):
        if not 0 <= int(v) <= 255:
            raise ValueError("LED values must be 0..255")
    acked = mouse.write(P.ADDR_LED, P.encode_led(mode, rgb, speed, light), wait=wait)
    want = {"mode": mode, "color": rgb, "speed": speed, "light": light}
    return _result(acked, _check(lambda: mouse.read_led() == want))


def set_rate(mouse, g24, bt, wait=0.0):
    """g24 / bt: 125, 250, 500 or None (leave that half alone)."""
    for v in (g24, bt):
        if v is not None and v not in RATES:
            raise ValueError(f"rate must be one of {RATES}")
    cfg = P.decode_config(mouse.read_config())
    cur = cfg["rate_byte"]
    if cur is None:
        raise P.ZeloError("current rate byte failed its checksum, refusing to write")
    new = P.encode_rate(g24, bt, cur)
    if new == cur:
        return _result(True, True, changed=False)
    path = cli.backup(mouse)
    acked = mouse.write_rate_byte(new, wait=wait)
    verified = _check(lambda: P.decode_config(mouse.read_config())["rate_byte"] == new)
    return _result(acked, verified, backup=path)


def set_dpi(mouse, values, count, stage, wait=0.0):
    """values: 5 DPI numbers (None = leave the slot alone); count 1..5; stage 0-based."""
    if len(values) != P.DPI_SLOTS:
        raise ValueError(f"need {P.DPI_SLOTS} DPI values")
    for v in values:
        if v is not None and v not in P.SENSOR_DPI:
            raise ValueError(f"DPI {v} not supported (400..2400 step 100)")
    if not 1 <= count <= P.DPI_SLOTS or not 0 <= stage < count:
        raise ValueError("bad stage count / current stage")
    cfg = P.decode_config(mouse.read_config())
    if cfg["stages"] is None or cfg["level"] is None:
        raise P.ZeloError("stage count/level failed their checksum, refusing to write")

    todo_dpi = [i for i, v in enumerate(values) if v is not None and cfg["dpi"][i]["dpi"] != v]
    if not todo_dpi and count == cfg["stages"] and stage == cfg["level"]:
        return _result(True, True, changed=False)
    path = cli.backup(mouse)
    ok = True
    for i in todo_dpi:
        ok = mouse.write_dpi(i, values[i], wait=wait) and ok
    if count != cfg["stages"]:
        ok = mouse.write_stages(count, wait=wait) and ok
    if stage != cfg["level"]:
        ok = mouse.write_level(stage, wait=wait) and ok
    def same():
        c2 = P.decode_config(mouse.read_config())
        return (c2["stages"] == count and c2["level"] == stage
                and all(v is None or c2["dpi"][i]["dpi"] == v for i, v in enumerate(values)))

    return _result(ok, _check(same), backup=path)


def parse_action(text):
    """-> ('entry', (type, code, param)) or ('key', (mods, keys)); ValueError if bad."""
    t = text.lower()
    if t.startswith("key:"):
        return "key", P.parse_keystroke(t[4:])
    if t in P.MOUSE_BUTTONS:
        return "entry", (P.TYPE_BUTTON, P.MOUSE_BUTTONS[t], 0)
    if t in P.FUNCTIONS:
        return "entry", (*P.FUNCTIONS[t], 0)
    raise ValueError(f"unknown action '{text}'")


def bind(mouse, slot, action, wait=0.0, force=False):
    kind, spec = parse_action(action)
    if not 0 <= slot < P.KEY_SLOTS:
        raise ValueError(f"slot must be 0..{P.KEY_SLOTS - 1}")
    cur = P.decode_config(mouse.read_config())["keys"][slot]
    if not cur["ok"]:
        raise P.ZeloError("this slot failed its checksum, refusing to write")
    if cur["type"] == P.TYPE_MACRO and not force:
        if P.decode_key_macro(mouse.read_macro_header(cur["code"])) is None:
            raise P.ZeloError("this slot holds a macro zelo can't decode (use force to overwrite)")
    path = cli.backup(mouse)
    if kind == "key":
        ok = mouse.write_key_macro(slot, *spec, wait=wait)
        ok = mouse.write_key(slot, P.TYPE_MACRO, slot, 1, wait=wait) and ok
    else:
        ok = mouse.write_key(slot, *spec, wait=wait)

    def same():
        now = P.decode_config(mouse.read_config())["keys"][slot]
        want = (P.TYPE_MACRO, slot) if kind == "key" else spec[:2]
        if not (now["ok"] and (now["type"], now["code"]) == want):
            return False
        if kind == "key":
            return P.decode_key_macro(mouse.read_macro_header(slot)) == (spec[0], list(spec[1]))
        return True

    return _result(ok, _check(same), backup=path)


def summarize(results):
    """One-line status (Russian) for a list of write results."""
    results = [r for r in results if r["changed"]]
    if not results:
        return "Изменений нет"
    if all(r["verified"] for r in results):
        return "Применено ✓"
    if not all(r["acked"] for r in results):
        return "Нет подтверждения от мыши (спит?). Подвигайте ею и нажмите «Обновить»"
    return "Записано, но чтение назад отличается. Нажмите «Обновить»"
