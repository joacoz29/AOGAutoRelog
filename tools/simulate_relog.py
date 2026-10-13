"""Simulacion del relog completo con capturas reales (sin tocar el juego ni la red).

Uso: python tools/simulate_relog.py <carpeta con capturas> [--lose-start] [--slow-start]
La carpeta debe tener: disc.png server.png chan1.png chan4.png pin.png ann.png char.png trade.png world.png
(capturas de pantalla completa del juego, 1920x1032). Un 'juego falso' pasa de pantalla segun donde se hace click.
"""
import sys, time as _time, logging, types
from pathlib import Path
import cv2, numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import autorelog as c

folder = Path(sys.argv[1]); lose = "--lose-start" in sys.argv
slow = "--slow-start" in sys.argv   # tras un Start correcto la pantalla sigue IGUAL ~2.4 s (espera del servidor), como en el juego real
F = {}
for n in ("disc", "server", "chan1", "chan4", "pin", "ann", "char", "trade", "world"):
    im = cv2.imread(str(folder / f"{n}.png"))[:1032, :1919]
    F[n] = cv2.copyMakeBorder(im, 0, 1032 - im.shape[0], 0, 1920 - im.shape[1], cv2.BORDER_CONSTANT)

clock = [1000.0]
_time.time = lambda: clock[0]
_time.sleep = lambda s: clock.__setitem__(0, clock[0] + s)
c.time = _time

cfg = dict(c.load_cfg(), dry_run=False, angel_baby_after_relog=False, exp_tracking=False, capture_unknown=False,
           restore_minimized=False, channel_fixed=4)
bot = c.Relog(cfg)
st = {"s": "disc", "since": clock[0], "clicks": [], "lost": lose}
log = []
logging.getLogger("autorelog").setLevel(logging.CRITICAL)

def hit_box(name, x, y):  # el click cae dentro del boton de la plantilla?
    g = cv2.cvtColor(F["chan1" if st["s"] == "chan4" else st["s"]], cv2.COLOR_BGR2GRAY)   # en chan4 el cursor tapa el boton
    for v, t in c.variants(name):
        r = cv2.matchTemplate(g, t, cv2.TM_CCOEFF_NORMED); _, mx, _, loc = cv2.minMaxLoc(r)
        if mx > .8 and loc[0] - 4 <= x <= loc[0] + t.shape[1] + 4 and loc[1] - 4 <= y <= loc[1] + t.shape[0] + 4:
            return True
    return False

def go(s, why=""):
    st["s"] = s; st["since"] = clock[0]; log.append((round(clock[0] - 1000, 1), s, why))

def fake_click(hwnd, x, y, cfg_, level=0):
    s = st["s"]; st["clicks"].append((round(clock[0] - 1000, 1), s, int(x), int(y), level))
    if s == "disc" and hit_box("ok_btn", x, y): go("server")
    elif s == "server" and hit_box("start_btn", x, y):
        if st["lost"]: st["lost"] = False       # el primer click de Start se pierde
        elif slow: st.setdefault("srv_pending", clock[0])   # registra, pero la pantalla no cambia todavia
        else: go("chan1")
    elif s == "chan1" and 509 <= y <= 548: go("chan4")
    elif s == "chan4" and hit_box("chan_ok", x, y): st["pending"] = clock[0]
    elif s == "pin" and hit_box("ok_btn", x, y): go("ann")
    elif s == "ann" and hit_box("confirm_btn", x, y): go("char")
    elif s == "char" and hit_box("enter_btn", x, y): st["entering"] = clock[0]
    elif s == "trade" and hit_box("tradepin_no", x, y): go("world")

def tick():
    # transiciones por tiempo (carga del juego)
    if st["s"] == "server" and st.get("srv_pending") and clock[0] - st["srv_pending"] > 2.4: st.pop("srv_pending"); go("chan1")
    if st["s"] == "chan4" and st.get("pending") and clock[0] - st["pending"] > 2: st.pop("pending"); go("pin")
    if st["s"] == "char" and st.get("entering") and clock[0] - st["entering"] > 2: st.pop("entering"); go("trade")

c.find_window = lambda cfg_: 1
c.win32gui.IsIconic = lambda h: False
c.win32gui.GetForegroundWindow = lambda: 2
c.win32gui.GetWindowText = lambda h: ("Angels Online Global - x@gg(Athena-4)" if st["s"] in ("trade", "world") else "Angels Online Global")
c.grab = lambda h: F[st["s"]]
c.click = fake_click
c.online = lambda timeout=2.0: True
c._sleep = c._wake = lambda *a, **k: None
c.netmon.record_game_event = lambda *a, **k: None

bot.outage_probe = lambda: 0.0
t0 = clock[0]; done = None
for _ in range(150):
    tick(); bot.step(); tick()
    clock[0] += bot.next_delay if bot.next_delay < 1 else 0.5
    if st["s"] == "world" and bot.drop_start is None and not bot.ab_pending and clock[0] - t0 > 5:
        done = clock[0] - t0; break
print("pantallas:", " > ".join(f"{s}({t}s)" for t, s, _ in log))
print("clicks:")
for t, s, x, y, lv in st["clicks"]: print(f"  t={t:5.1f}s  en {s:7s} ({x},{y}) nivel {lv}")
print(f"RESULTADO: {'volvio al mundo' if done else 'NO LLEGO AL MUNDO'} | duracion simulada: {done:.1f}s" if done else "RESULTADO: NO LLEGO AL MUNDO")
print("clicks totales:", len(st["clicks"]))
