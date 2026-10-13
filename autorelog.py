"""AutoRelog: vuelve a entrar a Angels Online tras un corte de conexion.

Mira la ventana del juego (captura en segundo plano), reconoce en que pantalla esta
y hace los clicks necesarios hasta volver al mundo. Recuerda el ultimo canal.

Uso:
    autorelog.py                  # vigila (en modo ensayo si config.dry_run = true)
    autorelog.py --live           # vigila y hace clicks de verdad
    autorelog.py --once           # un solo analisis de la ventana y sale
    autorelog.py --image foto.png # analiza una captura estatica, sin ventana ni clicks
"""
import argparse
import ctypes
import json
import logging
import os
import re
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import psutil
import win32api
import win32con
import win32gui
import win32process
import win32ui

import expmon
import netmon

ctypes.windll.user32.SetProcessDPIAware()

ROOT = Path(__file__).parent
CFG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
TEMPLATES = ROOT / "templates"

DEFAULTS = {
    "dry_run": True,
    "input": "background",  # background (mensajes al juego, no toca tu mouse) | foreground (mouse real)
    "poll_seconds": 3,        # revision en el mundo (reposo)
    "fast_poll": 0.35,        # revision mientras reloguea
    "action_cooldown": 0.3,
    "max_attempts": 8,
    "default_channel": 4,
    "threshold": 0.8,
    "server_early_retry": False,  # reintentar Start apenas la pantalla sigue identica (EXPERIMENTAL: hace clicks de mas si el servidor tarda con la pantalla quieta; por ahora solo mide)
    "server_early_probe": 1.0,  # s tras clickear Start antes de mirar si la pantalla cambio (carga); si no cambio, reintenta sin esperar el MIN_REPEAT
    "dismiss_trade_pin": True,
    "channel_fixed": 0,    # 0 = recordar el ultimo canal
    "autostart": True,     # la interfaz empieza a vigilar al abrirse
    "minimize_to_tray": True,        # la X oculta la ventana en la bandeja en vez de cerrar
    "tray_notifications": True,      # avisos de la bandeja con la ventana oculta
    "start_in_tray": False,          # arrancar sin mostrar la ventana
    "restore_minimized": True,       # si el juego esta minimizado, restaurarlo (sin foco) cuando haga falta revisarlo
    "peek_min_outage": 3,            # segundos de corte de internet a partir de los cuales se revisa el juego
    "peek_during_outage_s": 45,      # si el internet lleva tanto caido y el juego esta minimizado, restaurarlo ya (0 = no)
    "outage_front": True,            # al cortarse el internet: dejar el juego abierto AL FRENTE (False = de fondo, detras de las demas ventanas)
    "peek_dwell_s": 45,              # tras un corte, cuanto dejarlo restaurado mirando antes de volver a minimizarlo
    "peek_every_min": 10,            # ademas, revisarlo cada tantos minutos si sigue minimizado (0 = nunca)
    "exp_tracking": True,            # leer la experiencia del HUD y calcular el ritmo por hora
    "exp_stall_min": 10,             # avisar si pasan estos minutos sin ganar experiencia (0 = nunca)
    "capture_unknown": True,         # guardar en capturas/ las pantallas del login no reconocidas (para armar plantillas)
    "isp": "",                       # nombre del proveedor (aparece en el informe de conexion)
    "fallback_real_mouse": True,     # si un paso falla 2 veces en segundo plano, ese click va con el mouse real
    "angel_baby_after_relog": True,  # al volver al mundo: Ctrl+R y tildar Attack
    "ab_close_panel": True,          # cerrar el panel del Angel Baby al terminar
    "ab_delay": 2,                   # segundos en el mundo antes de activarlo
    "process": "angel.dat",
    "title_prefix": "Angels Online Global",
}
SCALES = [1.0, 0.95, 1.05, 0.9, 1.1, 0.85, 1.15, 0.8, 1.2, 1.3, 0.7]
ACTIVE_SCALES = [1.0]    # el juego dibuja su interfaz 1:1 en cualquier resolucion: buscar a escala 1 es ~10 veces mas rapido
WIDE_SCALES = False      # se activa si la interfaz aparece escalada (p. ej. un modo que estira la imagen)
# s antes de repetir el mismo click: el servidor tarda 2-4 s en responder y el mundo en cargar; repetir antes solo suma clicks de mas
# (el click de Start a veces se pierde justo cuando la pantalla aparece: se reintenta rapido; lo que ya cambio de pantalla no se repite)
MIN_REPEAT = {"server": 3.0, "channel": 4.0, "character": 4.5, "disconnect": 3.0, "connfail": 3.0, "pin": 2.5, "announcement": 2.5}
TITLE_RE = re.compile(r"\((?P<server>[^()-]+)-(?P<ch>\d+)\)\s*$")

# Reglas en orden de prioridad. marker: texto que identifica la pantalla/cartel;
# button: boton a apretar (se busca debajo del marker). world: tambien vale dentro del mundo.
RULES = [
    dict(name="disconnect", marker="disc_text", button="ok_btn", world=True),
    dict(name="pin", marker="pin_text", button="ok_btn"),
    dict(name="tradepin", marker="tradepin_text", button="tradepin_no", world=True, opt="dismiss_trade_pin"),
    dict(name="announcement", marker=None, button="confirm_btn"),
    dict(name="connfail", marker="fail_text", button="ok_btn"),   # "Failed to connect to server." (servidor caido / mantenimiento)
    dict(name="server", marker="server_hdr", button="start_btn"),
    dict(name="channel", marker="channel_hdr", button="chan_ok"),
    dict(name="character", marker=None, button="enter_btn"),
]

# Offsets medidos en capturas a escala 1:1, relativos a la esquina sup-izq del recorte
# Distancias medidas por estilo de dibujo ("sd" viejo / "hd" Render Fix): fila Athena(1) y boton aceptar respecto de la
# esquina de channel_hdr. En "hd" la lista es mas compacta (19 px por fila en vez de 20.25).
CHANNEL_LAYOUT = {
    "sd": dict(row0=(108, 29), pitch=20.25, ok=(136, 299)),
    "hd": dict(row0=(92, 28), pitch=19.0, ok=(120, 280)),
}

# Panel del Angel Baby: offsets desde la esquina sup-izq de la plantilla "ab_enable" (texto Enable Angel Baby)
AB_LAYOUT = {
    "sd": dict(tab=(5, -14), attack=(-9, -469), enable=(-9, 9), close=(396, 9)),
    "hd": dict(tab=(9, -16), attack=(-8, -442), enable=(-8, 7), close=(372, 6)),   # el panel es mas compacto en este estilo
}
AB_TABS_TO_ENABLE = {"sd": (-39, 24), "hd": (-37, 24)}   # de la esquina de ab_tabs a la de ab_enable

log = logging.getLogger("autorelog")


def _atomic_write(path, text):
    """Escribe a un temporal y lo mueve: si se corta la luz a mitad, el archivo viejo queda intacto."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_cfg():
    """Lee config.json; si esta danado prueba con la copia (.bak) y si no con los valores por defecto."""
    cfg = dict(DEFAULTS)
    for p in (CFG_PATH, CFG_PATH.with_name(CFG_PATH.name + ".bak")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
                break
        except (OSError, ValueError):
            continue
    return cfg


def save_cfg(cfg):
    try:  # copia de la version anterior (solo si era valida)
        json.loads(CFG_PATH.read_text(encoding="utf-8"))
        shutil.copyfile(CFG_PATH, CFG_PATH.with_name(CFG_PATH.name + ".bak"))
    except (OSError, ValueError):
        pass
    _atomic_write(CFG_PATH, json.dumps(cfg, indent=2))


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    _atomic_write(STATE_PATH, json.dumps(state, indent=2))


def file_handler():
    """Registro en autorelog.log con rotacion (1 MB x 3) para que no crezca sin limite."""
    from logging.handlers import RotatingFileHandler
    h = RotatingFileHandler(ROOT / "autorelog.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(message)s", "%H:%M:%S"))
    return h


_tpl_cache = {}
PREFERRED = ["sd"]   # estilo de plantillas que coincidio la ultima vez: se prueba primero


def variants(name):
    """[(estilo, imagen)]: templates/<n>.png ('sd', estilo viejo) y templates/hd/<n>.png ('hd', texto nitido del Render Fix)."""
    if name not in _tpl_cache:
        out = []
        for v, path in (("sd", TEMPLATES / f"{name}.png"), ("hd", TEMPLATES / "hd" / f"{name}.png")):
            img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE) if path.exists() else None
            if img is not None:
                out.append((v, img))
        if not out:
            raise FileNotFoundError(TEMPLATES / f"{name}.png")
        _tpl_cache[name] = out
    return _tpl_cache[name]


def template(name):
    return variants(name)[0][1]


def find(gray, name, thr, roi=None):
    """Mejor coincidencia de la plantilla (en cualquiera de sus estilos). Devuelve dict (con 'v' = estilo) o None."""
    ox = oy = 0
    if roi:
        x0, y0, x1, y1 = [max(0, int(v)) for v in roi]
        gray = gray[y0:y1, x0:x1]
        ox, oy = x0, y0
    for v, tpl in sorted(variants(name), key=lambda p: p[0] != PREFERRED[0]):
        best = None
        for s in ACTIVE_SCALES:
            t = tpl if s == 1.0 else cv2.resize(tpl, None, fx=s, fy=s, interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
            if t.shape[0] >= gray.shape[0] or t.shape[1] >= gray.shape[1]:
                continue
            res = cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(res)
            if best is None or score > best["score"]:
                best = dict(score=float(score), x=loc[0] + ox, y=loc[1] + oy, w=t.shape[1], h=t.shape[0], s=s, v=v)
            if score >= 0.95:
                break
        if best and best["score"] >= thr:
            PREFERRED[0] = v
            return best
    return None


def center(m):
    return m["x"] + m["w"] / 2, m["y"] + m["h"] / 2


def detect(gray, in_world, cfg):
    """Devuelve (regla, [puntos de click]) para la primera regla que coincide, o None."""
    thr = cfg["threshold"]
    for r in RULES:
        if in_world and not r.get("world"):
            continue
        if r.get("opt") and not cfg.get(r["opt"], True):
            continue
        if r["name"] == "channel":
            h = find(gray, "channel_hdr", max(thr, 0.9))  # se parece a "Select Server"
            if not h:
                continue
            return r, h
        if r["marker"]:
            mk = find(gray, r["marker"], thr)
            if not mk:
                continue
            s = mk["s"]
            roi = (mk["x"] - 120 * s, mk["y"], mk["x"] + 320 * s, mk["y"] + 420 * s)
            btn = find(gray, r["button"], thr, roi)
            if not btn:
                log.debug("marker %s visto (%.2f) pero sin boton %s", r["marker"], mk["score"], r["button"])
                continue
            return r, btn
        btn = find(gray, r["button"], thr)
        if btn:
            return r, btn
    return None


# ---------------------------------------------------------------- ventana / captura

def find_window(cfg):
    out = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h).startswith(cfg["title_prefix"]):
            try:
                _, pid = win32process.GetWindowThreadProcessId(h)
                if psutil.Process(pid).name().lower() == cfg["process"].lower():
                    out.append(h)
            except psutil.Error:
                pass

    win32gui.EnumWindows(cb, None)
    return out[0] if out else None


def grab(hwnd):
    """Captura el area cliente via PrintWindow (funciona aunque la tapen otras ventanas)."""
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
    cw, ch = win32gui.GetClientRect(hwnd)[2:]
    if w <= 0 or h <= 0 or cw <= 0 or ch <= 0 or win32gui.IsIconic(hwnd):
        return np.zeros((0, 0, 3), np.uint8)   # minimizada justo ahora: no hay nada que capturar
    hdc = win32gui.GetWindowDC(hwnd)
    src = win32ui.CreateDCFromHandle(hdc)
    mem = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(src, w, h)
    mem.SelectObject(bmp)
    ctypes.windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), 2)
    buf = bmp.GetBitmapBits(True)
    win32gui.DeleteObject(bmp.GetHandle())
    mem.DeleteDC()
    src.DeleteDC()
    win32gui.ReleaseDC(hwnd, hdc)
    img = np.frombuffer(buf, np.uint8).reshape(h, w, 4)
    ox, oy = cx - l, cy - t
    return np.ascontiguousarray(img[oy:oy + ch, ox:ox + cw, :3])


SW_SHOWNOACTIVATE, SW_SHOWMINNOACTIVE = 4, 7  # restaurar / minimizar sin quitarte el foco


def bring_to_front(hwnd):
    """Restaura (si esta minimizada) y pone la ventana al frente, con foco."""
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)   # truco para poder tomar el foco
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:  # noqa: BLE001
        log.warning("no pude poner el juego al frente: %s", e)


def send_to_back(hwnd):
    """Detras de las demas ventanas, sin activarla ni moverla. Capturar con PrintWindow funciona igual aunque este tapada;
    en pantalla completa sin bordes, restaurarla encima cubriria todo el escritorio."""
    try:
        win32gui.SetWindowPos(hwnd, win32con.HWND_BOTTOM, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
    except Exception:  # noqa: BLE001
        pass
PARK = (900, 30)  # rincon vacio donde dejamos el cursor del juego despues de cada click
SETTLE = 0.9  # el juego procesa la entrada en su siguiente cuadro; no marcarlo inactivo antes


def _send(hwnd, msg, wparam=0, lparam=0):
    """SendMessage con tope de tiempo (se ejecuta dentro del hilo del juego)."""
    try:
        win32gui.SendMessageTimeout(hwnd, msg, wparam, lparam, win32con.SMTO_ABORTIFHUNG, 800)
    except Exception:  # noqa: BLE001
        pass


def _wake(hwnd):
    """El juego ignora mouse y teclado si no cree estar activo. Se lo hacemos creer sin tocar el foco real."""
    tid = win32process.GetWindowThreadProcessId(hwnd)[0]
    _send(hwnd, win32con.WM_ACTIVATEAPP, 1, tid)
    _send(hwnd, win32con.WM_NCACTIVATE, 1, 0)
    _send(hwnd, win32con.WM_ACTIVATE, win32con.WA_ACTIVE, 0)
    _send(hwnd, win32con.WM_SETFOCUS, 0, 0)


def _sleep(hwnd):
    """Deja al juego en el estado 'inactivo' que tenia (solo si realmente no esta al frente)."""
    _send(hwnd, win32con.WM_KILLFOCUS, 0, 0)
    _send(hwnd, win32con.WM_ACTIVATE, win32con.WA_INACTIVE, 0)
    _send(hwnd, win32con.WM_NCACTIVATE, 0, 0)
    _send(hwnd, win32con.WM_ACTIVATEAPP, 0, 0)


def bg_click(hwnd, x, y, sleep=True, press=0.1, moves=1):
    """Click en segundo plano: no mueve el mouse real ni toma el foco."""
    lp = win32api.MAKELONG(int(x), int(y))
    was_fg = win32gui.GetForegroundWindow() == hwnd
    _wake(hwnd)
    for i in range(moves):  # varios movimientos: el juego detecta el 'hover' por cuadro
        _send(hwnd, win32con.WM_MOUSEMOVE, 0, win32api.MAKELONG(int(x) + (i % 2), int(y)))
        time.sleep(0.12)
    _send(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
    time.sleep(press)  # el juego lee el mouse por cuadro: en pantallas de login (pocos FPS) un click corto se pierde
    _send(hwnd, win32con.WM_LBUTTONUP, 0, lp)
    time.sleep(0.05)
    _send(hwnd, win32con.WM_MOUSEMOVE, 0, win32api.MAKELONG(*PARK))
    if sleep and not was_fg:
        time.sleep(SETTLE)
        _sleep(hwnd)
    else:
        time.sleep(0.1)


def bg_keys(hwnd, mods, vk, hold=0.3, sleep=True):
    """Combinacion de teclas en segundo plano, p.ej. bg_keys(h, [VK_CONTROL], ord('R'))."""
    was_fg = win32gui.GetForegroundWindow() == hwnd

    def key(k, down):
        sc = win32api.MapVirtualKey(k, 0)
        lp = 1 | (sc << 16) | (0 if down else (1 << 30 | 1 << 31))
        _send(hwnd, win32con.WM_KEYDOWN if down else win32con.WM_KEYUP, k, lp)

    _wake(hwnd)
    for m in mods:
        key(m, True)
        time.sleep(0.15)
    key(vk, True)
    time.sleep(hold)  # el juego lee las teclas por cuadro: si la pulsacion es muy corta puede perderse
    key(vk, False)
    for m in reversed(mods):
        time.sleep(0.1)
        key(m, False)
    if sleep and not was_fg:
        time.sleep(SETTLE)
        _sleep(hwnd)
    else:
        time.sleep(0.1)


def grab_pair(hwnd):
    """(captura en color, captura en gris). Ajusta las escalas de busqueda segun el tamano de la ventana."""
    global ACTIVE_SCALES
    img = grab(hwnd)
    ACTIVE_SCALES = SCALES if WIDE_SCALES else [1.0]
    if img.size == 0:
        return img, np.zeros((1, 1), np.uint8)
    return img, cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def grab_gray(hwnd):
    return grab_pair(hwnd)[1]


METHODS = ["fondo", "fondo largo", "mouse real"]


def real_click(hwnd, x, y):
    """Click con el mouse real: trae el juego al frente un instante y despues devuelve foco y cursor."""
    x, y = int(x), int(y)
    prev_fg = win32gui.GetForegroundWindow()
    prev_pos = win32api.GetCursorPos()
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.4)
    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)  # truco para poder tomar el foco
    win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:  # noqa: BLE001
        log.warning("no pude traer la ventana al frente: %s", e)
    time.sleep(0.2)
    sx, sy = win32gui.ClientToScreen(hwnd, (x, y))
    win32api.SetCursorPos((sx, sy))
    time.sleep(0.1)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.15)
    win32api.SetCursorPos(prev_pos)
    if prev_fg and prev_fg != hwnd and win32gui.IsWindow(prev_fg):
        try:
            win32gui.SetForegroundWindow(prev_fg)
        except Exception:  # noqa: BLE001
            pass


def click(hwnd, x, y, cfg, level=0):
    """level 0: fondo (pulsacion corta) | 1: fondo largo | 2: mouse real (si esta permitido)."""
    x, y = int(x), int(y)
    if cfg["input"] == "foreground":
        real_click(hwnd, x, y)
    elif level <= 0:
        bg_click(hwnd, x, y, sleep=False)
    elif level == 1 or not cfg.get("fallback_real_mouse", True):
        bg_click(hwnd, x, y, sleep=False, press=0.4, moves=3)
    else:
        real_click(hwnd, x, y)


def online(timeout=2.0):
    for host in (("1.1.1.1", 443), ("8.8.8.8", 53)):
        try:
            socket.create_connection(host, timeout).close()
            return True
        except OSError:
            continue
    return False


def find_ab(gray, thr):
    """Ubica el panel del Angel Baby: por el texto 'Enable Angel Baby' o, si algo lo tapa, por la fila de pestanas."""
    m = find(gray, "ab_enable", thr)
    if m:
        return m
    t = find(gray, "ab_tabs", 0.75)
    if t:
        v = t.get("v", "sd")
        dx, dy = AB_TABS_TO_ENABLE[v]
        return dict(score=t["score"], x=t["x"] + dx, y=t["y"] + dy, w=119, h=19, s=1.0, v=v)
    return None


def is_checked(img_bgr, cx, cy):
    """Casilla tildada = marca naranja dentro del cuadradito (en BGR)."""
    roi = img_bgr[max(0, int(cy) - 8):int(cy) + 9, max(0, int(cx) - 8):int(cx) + 9].reshape(-1, 3).astype(int)
    b, g, r = roi[:, 0], roi[:, 1], roi[:, 2]
    return int(((r > 200) & (g > 110) & (g < 200) & (b < 90)).sum()) >= 10


# ---------------------------------------------------------------- logica

class Relog:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = load_state()
        self.history = {}      # regla -> [timestamps]
        self.paused_until = 0
        self.last_status = None
        self.info = dict(window=False, title="", in_world=False, phase="idle", acted=0, exp=None)
        self.ab_pending = False   # activar el Angel Baby cuando se vuelva a estar en el mundo
        self.drop_start = None    # cuando aparecio el cartel de desconexion (para medir cuanto tarda en volver)
        self.want_peek = False    # pedido de revisar el juego (lo activa el monitor de internet al volver la conexion)
        self.peek_reason = ""
        self.restored_by_us = False  # restauramos nosotros la ventana minimizada: hay que volver a minimizarla
        self.peek_started = 0.0
        self.restore_wait = 0.0
        self.last_peek = time.time()
        self.outage_probe = None      # callable -> segundos que lleva el corte de internet actual (0 si no hay); lo pone la interfaz
        self._peek_outage_done = False
        self._front_done = False      # ya se puso el juego al frente en este corte
        self.followups = []           # horas de revisiones de seguimiento tras un corte largo
        self.peek_dwell = 6.0         # segundos que se queda restaurado antes de volver a minimizar
        self.world_since = None
        self.ab_tries = 0
        self.server_retry_at = 0.0     # tras clickear Start: piso antes de volver a mirar si avanzo (reintento rapido)
        self.server_probe_until = 0.0  # fin de la ventana de reintento rapido tras un Start
        self._srv_base = None          # cuadro de Select Server justo antes del click con el que se compara
        self.awake = False         # el juego quedo 'despierto' tras una secuencia de clicks
        self.last_act = {}         # regla -> momento del ultimo click
        self.act_hist = {}         # regla -> clicks recientes (para escalar el metodo si no avanza)
        self.next_delay = cfg["poll_seconds"]
        self.drop_login_seen = False  # tras el cartel de desconexion, ya se vio la pantalla de login
        self.unk_n = 0                # cuadros seguidos sin reconocer fuera del mundo
        self.connfail_n = 0           # "Failed to connect" seguidos: el servidor esta caido (mantenimiento)
        self.server_hold_until = 0.0  # no volver a apretar Start antes de esta hora
        self._rec_t = 0.0             # registro de capturas no reconocidas
        self._rec_n = 0
        self._rec_last = None
        self.expect_channel = None    # canal que se eligio en el relog: al entrar al mundo se comprueba que sea ese
        self.ignore_ch = None         # canal "equivocado" ya avisado: no se guarda como preferido
        self.relog_t = 0.0
        self._lock = threading.RLock()  # el ciclo, "Probar" y "Angel Baby" no deben clickear a la vez
        try:
            self.exp_reader = expmon.DigitReader()
        except (OSError, ValueError):
            self.exp_reader = None      # falta templates/exp_digits.npz: la experiencia no se mide
        self.exp = expmon.ExpTracker()
        self._exp_t = 0.0
        self._exp_fail = 0

    def reset(self):
        """Al (re)iniciar la vigilancia: olvida pausas y reintentos de antes."""
        self.paused_until = 0
        self.history, self.last_act, self.act_hist = {}, {}, {}
        self.drop_start = None
        self.drop_login_seen = False
        self.connfail_n, self.server_hold_until = 0, 0.0
        self._rec_n = 0
        self.expect_channel = self.ignore_ch = None
        self.want_peek = self.restored_by_us = False
        self._peek_outage_done = False
        self.followups = []
        self.last_peek = time.time()
        self.ab_pending = False
        self.world_since = None
        self.server_retry_at = self.server_probe_until = 0.0
        self._srv_base = None

    def _server_stuck(self, gray):
        """True solo si tras clickear Start la pantalla de *Select Server* sigue igual que ANTES del
        click (pixel a pixel, casi). Tras un Start exitoso el juego tarda ~2.4 s en mostrar el canal
        y durante ese rato cambia aunque sea un poco (fundido de `transition`, cursor); si nada cambio
        el click no registro y conviene reintentar ya, sin esperar el MIN_REPEAT."""
        if self._srv_base is None or find(gray, "server_hdr", self.cfg["threshold"]) is None:
            return False
        sig = cv2.resize(gray, (96, 54), interpolation=cv2.INTER_AREA).astype(np.int16)
        diff = int(np.abs(sig - self._srv_base).max())
        changed = diff > 6   # cualquier zona que se mueva = transicion
        self._srv_base = None
        # MEDICION: queda en el registro para calibrar con relogs reales (¿una espera normal del servidor deja la pantalla quieta?)
        log.info("Start: diferencia máxima %d a %.1f s del click (%s)", diff,
                 time.time() - (self.server_probe_until - 6.0), "cambió" if changed else "idéntica")
        return not changed   # identica a antes del click: el click no registro

    def _exp_tick(self, bgr):
        """Cada ~10 s, con el juego en el mundo y sin carteles: lee el % de experiencia del HUD."""
        if not self.cfg.get("exp_tracking", True) or self.exp_reader is None:
            return
        now = time.time()
        if now - self._exp_t < 10:
            return
        self._exp_t = now
        pct = self.exp_reader.read_pct(bgr)
        if pct is None:
            self._exp_fail += 1
            self.info["exp"] = None
            if self._exp_fail == 6:  # una sola vez por racha
                log.warning("no puedo leer la experiencia del HUD (¿la ventana no mide 1366x768 o hay algo encima?)")
            return
        self._exp_fail = 0
        self.info["exp"] = (pct, now)
        kind = self.exp.feed(pct, now)
        if kind == "lvl":
            log.info("¡Subiste de nivel! (experiencia %.2f %%)", pct)
        elif kind == "loss":
            log.warning("Bajó la experiencia a %.2f %% (¿murió el personaje?)", pct)
        minutes = self.cfg.get("exp_stall_min", 10)
        if minutes and minutes > 0:
            s = self.exp.should_notify_stall(minutes, now)
            if s:
                log.warning("Sin ganar experiencia hace %d min: ¿Angel Baby parado o personaje muerto?", s // 60)

    def _record(self, bgr, tag):
        """Guarda en capturas/ un cuadro que no se reconocio (si cambio respecto del ultimo guardado). Maximo 40 por inicio."""
        if not self.cfg.get("capture_unknown", True) or self._rec_n >= 60:
            return
        now = time.time()
        if now - self._rec_t < 1.0:
            return
        small = cv2.resize(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), (96, 54), interpolation=cv2.INTER_AREA).astype(np.int16)
        if self._rec_last is not None and float(np.abs(small - self._rec_last).mean()) < 6:
            return
        try:
            folder = ROOT / "capturas"
            folder.mkdir(exist_ok=True)
            cv2.imwrite(str(folder / f"{time.strftime('%H%M%S')}_{tag}.png"), bgr, [cv2.IMWRITE_PNG_COMPRESSION, 6])
        except OSError:
            return
        self._rec_t, self._rec_last = now, small
        self._rec_n += 1

    def save_frame(self, tag="manual"):
        """Guarda ya mismo la pantalla del juego en capturas/ (boton 'Capturar'). Devuelve la ruta o None."""
        hwnd = find_window(self.cfg)
        if not hwnd or win32gui.IsIconic(hwnd):
            log.warning("no puedo capturar: el juego no esta visible")
            return None
        folder = ROOT / "capturas"
        folder.mkdir(exist_ok=True)
        path = folder / f"{time.strftime('%H%M%S')}_{tag}.png"
        cv2.imwrite(str(path), grab(hwnd), [cv2.IMWRITE_PNG_COMPRESSION, 6])
        log.info("Captura guardada: capturas/%s", path.name)
        return path

    def status(self, msg):
        if msg != self.last_status:
            log.info(msg)
            self.last_status = msg

    def channel(self):
        if int(self.cfg.get("channel_fixed", 0)) > 0:
            return int(self.cfg["channel_fixed"])
        return int(self.state.get("channel", self.cfg["default_channel"]))

    def do(self, hwnd, rule, hit, gray=None, level=0):
        """Ejecuta (o ensaya) la accion de la regla. hit es el match del boton (o del header)."""
        name = rule["name"]
        dry = self.cfg["dry_run"]
        tag = "[ENSAYO] haria" if dry else "hago"
        mode = "mouse real" if self.cfg["input"] == "foreground" else METHODS[min(level, 2)]
        if name == "channel":
            s = hit["s"]
            L = CHANNEL_LAYOUT[hit.get("v", "sd")]
            ch = self.channel()
            rx = hit["x"] + L["row0"][0] * s
            ry = hit["y"] + (L["row0"][1] + L["pitch"] * (ch - 1)) * s
            ox = hit["x"] + L["ok"][0] * s
            oy = hit["y"] + L["ok"][1] * s
            log.info("%s: canal %d -> click fila (%d,%d), luego aceptar [%s]", tag, ch, rx, ry, mode)
            if not dry:
                chosen = False
                for tryn in range(3):   # el click en la fila a veces no registra: se verifica que quede marcada
                    click(hwnd, rx, ry, self.cfg, level)
                    time.sleep(0.35)
                    sel = self.selected_channel_row(grab(hwnd), hit)
                    if sel == ch:
                        chosen = True
                        break
                    log.info("canal: quedó marcada la fila %s en vez de la %d; reintento (%d/3)", sel, ch, tryn + 1)
                if not chosen:   # mejor no confirmar que entrar al canal que viene por defecto (suele estar lleno)
                    log.warning("no pude seleccionar el canal %d; no confirmo", ch)
                    return
                g = grab_gray(hwnd)
                ok = find(g, "chan_ok", self.cfg["threshold"],
                          (hit["x"], hit["y"] + 200 * s, hit["x"] + 320 * s, hit["y"] + 400 * s))
                if ok:
                    ox, oy = center(ok)
                click(hwnd, ox, oy, self.cfg, level)
                self.expect_channel, self.relog_t = ch, time.time()
            return
        x, y = center(hit)
        log.info("%s: %s -> click (%d,%d) [%s %.2f] [%s]", tag, name, x, y, rule["button"], hit["score"], mode)
        if not dry:
            click(hwnd, x, y, self.cfg, level)

    def selected_channel_row(self, img_bgr, hdr):
        """Numero (1-5) de la fila de canal marcada (fondo rojizo: R bastante mayor que B), o None."""
        s = hdr["s"]
        L = CHANNEL_LAYOUT[hdr.get("v", "sd")]
        best = None
        for n in range(1, 6):
            cy = hdr["y"] + (L["row0"][1] + L["pitch"] * (n - 1)) * s
            cx = hdr["x"] + L["row0"][0] * s
            roi = img_bgr[int(cy) - 5:int(cy) + 6, int(cx - 70 * s):int(cx + 70 * s)]
            if roi.size == 0:
                continue
            m = roi.reshape(-1, 3).mean(axis=0)
            rb = float(m[2] - m[0])
            if rb > 25 and (best is None or rb > best[0]):
                best = (rb, n)
        return best[1] if best else None

    def angel_baby_on(self, hwnd):
        """Ctrl+R, tilda Attack (y Enable si hiciera falta) y cierra el panel. Devuelve True si quedo activo."""
        thr = self.cfg["threshold"]
        if win32gui.IsIconic(hwnd):
            log.warning("el juego esta minimizado; no puedo activar el Angel Baby")
            return False
        gray = lambda: grab_gray(hwnd)  # noqa: E731
        self.awake = True

        def wait_panel(want_open, tries=16):
            for _ in range(tries):
                time.sleep(0.25)
                m = find_ab(gray(), thr)
                if bool(m) == want_open:
                    return m if want_open else True
            return None

        m = find_ab(gray(), thr)
        for _ in range(4):
            if m:
                break
            log.info("abriendo el Angel Baby (Ctrl+R)")
            bg_keys(hwnd, [win32con.VK_CONTROL], ord("R"), sleep=False)
            m = wait_panel(True)  # puede tardar si el juego esta ocupado; no mandar otro Ctrl+R (lo cerraria)
        if not m:
            cv2.imwrite(str(ROOT / "fallo_angelbaby.png"), grab(hwnd))
            log.warning("no pude abrir el panel del Angel Baby (captura en fallo_angelbaby.png)")
            return False
        mx, my = m["x"], m["y"]
        L = AB_LAYOUT[m.get("v", "sd")]
        if not find(gray(), "ab_attack", thr):  # solo si no estamos ya en la pestana Combat
            bg_click(hwnd, mx + L["tab"][0], my + L["tab"][1], sleep=False)
            time.sleep(0.3)
        ok = False
        for _ in range(3):
            if is_checked(grab(hwnd), mx + L["attack"][0], my + L["attack"][1]):
                ok = True
                break
            log.info("tildando Attack del Angel Baby")
            bg_click(hwnd, mx + L["attack"][0], my + L["attack"][1], sleep=False)
            time.sleep(0.5)
        if not ok:
            log.warning("no pude tildar Attack")
        elif not is_checked(grab(hwnd), mx + L["enable"][0], my + L["enable"][1]):
            log.info("tildando Enable Angel Baby")
            bg_click(hwnd, mx + L["enable"][0], my + L["enable"][1], sleep=False)
            time.sleep(0.4)
        if self.cfg.get("ab_close_panel", True):
            bg_click(hwnd, mx + L["close"][0], my + L["close"][1], sleep=False)  # boton X (el mouse es lo mas confiable)
            if not wait_panel(False, 12):
                bg_keys(hwnd, [win32con.VK_CONTROL], ord("R"), sleep=False)  # respaldo
                if not wait_panel(False, 12):
                    log.warning("el panel del Angel Baby no se cerro")
        return ok

    def handle_ab(self, hwnd):
        """Ya en el mundo tras un relog: espera un poco y activa el Angel Baby."""
        now = time.time()
        if self.world_since is None:
            self.world_since = now
        wait = self.cfg.get("ab_delay", 6) - (now - self.world_since)
        if wait > 0:
            self.status(f"en el mundo; activo el Angel Baby en {int(wait) + 1} s")
            return
        self.info["phase"] = "angelbaby"
        self.ab_tries += 1
        if self.cfg["dry_run"]:
            log.info("[ENSAYO] activaria el Angel Baby (Ctrl+R, tildar Attack)")
            ok = True
        else:
            self.status("activando el Angel Baby")
            ok = self.angel_baby_on(hwnd)
        if ok:
            log.info("Angel Baby activado" if not self.cfg["dry_run"] else "(ensayo) Angel Baby listo")
            self.ab_pending = False
        elif self.ab_tries >= 3:
            log.warning("no pude activar el Angel Baby tras %d intentos; lo dejo", self.ab_tries)
            self.ab_pending = False
        self.last_status = None

    def run_ab_now(self):
        with self._lock:
            return self._run_ab_now()

    def _run_ab_now(self):
        """Prueba manual desde la interfaz: activa el Angel Baby ya mismo (siempre de verdad)."""
        hwnd = find_window(self.cfg)
        if not hwnd:
            log.warning("no encuentro la ventana del juego")
            return False
        if not TITLE_RE.search(win32gui.GetWindowText(hwnd)):
            log.warning("el juego no esta dentro del mundo; no activo el Angel Baby")
            return False
        log.info("Angel Baby (prueba manual)")
        ok = self.angel_baby_on(hwnd)
        log.info("Angel Baby activado" if ok else "no se pudo activar el Angel Baby")
        return ok

    def throttled(self, name):
        now = time.time()
        h = [t for t in self.history.get(name, []) if now - t < 60]
        self.history[name] = h
        if len(h) >= self.cfg["max_attempts"]:
            log.warning("'%s' no avanza tras %d intentos; pauso 60 s", name, len(h))
            self.paused_until = now + 60
            self.history[name] = []
            return True
        h.append(now)
        return False

    def step(self):
        with self._lock:
            self._step()

    def _step(self):
        self.next_delay = self.cfg["poll_seconds"]
        if time.time() < self.paused_until:
            return
        hwnd = find_window(self.cfg)
        if not hwnd:
            self.info.update(window=False, title="", in_world=False, phase="nowindow")
            self.status("sin ventana del juego (angel.dat); esperando")
            return
        ongoing = self.outage_probe() if self.outage_probe else 0.0
        if ongoing <= 0:
            self._peek_outage_done = False
        if win32gui.IsIconic(hwnd):
            self.info.update(window=True, in_world=False, phase="minimized")
            if self.restored_by_us and time.time() - self.peek_started > 5:
                self.restored_by_us = False  # lo volvio a minimizar el usuario: ya no es cosa nuestra
            now = time.time()
            every = self.cfg.get("peek_every_min", 0)
            during = self.cfg.get("peek_during_outage_s", 0)
            reason = None
            outage_peek = True
            if self.want_peek:
                reason, dwell = self.peek_reason, self.cfg.get("peek_dwell_s", 45)
            elif during and ongoing >= during and not self._peek_outage_done:
                reason, dwell = f"el internet lleva {int(ongoing)} s caído", self.cfg.get("peek_dwell_s", 45)
                self._peek_outage_done = True
            elif self.followups and now >= self.followups[0]:
                reason, dwell = "revisión posterior al corte", self.cfg.get("peek_dwell_s", 45)
            elif every > 0 and now - self.last_peek >= every * 60:
                reason, dwell = "revisión periódica", 6.0
                outage_peek = False
            if reason and self.cfg.get("restore_minimized", True):
                self.want_peek = False
                self.followups = [t for t in self.followups if t > now]
                self.last_peek = now
                if self.cfg["dry_run"]:
                    log.info("[ENSAYO] restauraria el juego minimizado para revisarlo (%s)", reason)
                else:
                    if outage_peek:   # corte de internet: se deja abierto (no se vuelve a minimizar)
                        if self.cfg.get("outage_front", True):
                            log.info("el juego está minimizado: lo abro AL FRENTE para revisarlo (%s)", reason)
                            bring_to_front(hwnd)
                        else:
                            log.info("el juego está minimizado: lo abro DE FONDO para revisarlo (%s)", reason)
                            win32gui.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                            send_to_back(hwnd)
                        self._front_done = True
                        self.restore_wait = now + 1.5
                        self.next_delay = 0.4
                        return
                    log.info("el juego está minimizado: lo restauro sin darle el foco para revisarlo (%s)", reason)
                    win32gui.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                    send_to_back(hwnd)
                    self.restored_by_us = True
                    self.peek_dwell = dwell
                    self.peek_started = now
                    self.restore_wait = now + 1.5  # darle tiempo a dibujar antes de capturarlo
                    self.next_delay = 0.4
                    return
            self.status("el juego esta minimizado y no lo puedo ver (se restaura solo si se corta el internet)"
                        if self.cfg.get("restore_minimized", True) else
                        "el juego esta minimizado y no lo puedo ver: dejalo detras de otras ventanas, sin minimizar")
            return
        # juego visible y el internet recien se corto: al frente (o de fondo) una sola vez por corte
        if ongoing <= 0:
            self._front_done = False
        elif not self._front_done and ongoing >= self.cfg.get("peek_min_outage", 3):
            self._front_done = True
            if not self.cfg["dry_run"]:
                if self.cfg.get("outage_front", True):
                    log.info("se cortó el internet: dejo el juego al frente")
                    bring_to_front(hwnd)
                else:
                    log.info("se cortó el internet: dejo el juego de fondo")
                    send_to_back(hwnd)
        # con el juego visible no hace falta "revisarlo": que un pedido viejo no lo abra de golpe cuando lo minimices
        self.want_peek = False
        self.followups = [t for t in self.followups if t > time.time()]
        if not self.restored_by_us:
            self.last_peek = time.time()
        if self.restored_by_us:
            if ongoing > 0:
                self.peek_started = time.time()  # mientras dure el corte no se cuenta el tiempo de espera: corre desde que vuelva
            if time.time() < self.restore_wait:
                self.next_delay = 0.4
                return
            if win32gui.GetForegroundWindow() == hwnd:  # lo esta usando el usuario: no tocarlo
                self.restored_by_us = False
                log.info("estás usando el juego: no lo vuelvo a minimizar")
            elif time.time() - self.peek_started > 180:
                self.restored_by_us = False
                log.warning("el relog no terminó en 3 minutos: dejo el juego abierto para que lo veas")
        title = win32gui.GetWindowText(hwnd)
        m = TITLE_RE.search(title)
        in_world = bool(m)
        self.info.update(window=True, title=title, in_world=in_world, phase="world" if in_world else "unknown")
        if m:
            ch = int(m.group("ch"))
            if self.expect_channel is not None:
                if ch != self.expect_channel:
                    self.ignore_ch = ch
                    log.warning("Entré a %s-%d, pero el canal elegido era el %d (¿estaba lleno?). No lo guardo como preferido.",
                                m.group("server"), ch, self.expect_channel)
                self.expect_channel = None
            elif ch == self.ignore_ch:
                pass   # sigue en el canal equivocado ya avisado
            elif self.state.get("channel") != ch:
                self.ignore_ch = None
                self.state.update(channel=ch, server=m.group("server"))
                save_state(self.state)
                log.info("canal recordado: %s-%d", m.group("server"), ch)
        else:
            self.world_since = None
        bgr, gray = grab_pair(hwnd)
        if bgr.size == 0:   # la ventana se minimizo o cambio de tamano justo ahora
            self.next_delay = 0.5
            return
        if self.restored_by_us and float(bgr.std()) < 8.0:   # captura en negro/uniforme: el juego todavia no dibuja
            if time.time() - self.peek_started < 12:
                self.next_delay = 0.5
                return
            log.warning("restauré el juego pero no se ve su contenido (captura vacía); lo dejo abierto para que lo veas")
            self.restored_by_us = False
        elif self.restored_by_us and not getattr(self, "_peek_seen", False):
            self._peek_seen = True
            log.info("el juego restaurado ya dibuja: lo reviso")
        if not self.restored_by_us:
            self._peek_seen = False
        # Start perdido: si venimos de clickear en Select Server y a los ~1 s la pantalla sigue identica a
        # la de antes del click, reintentar sin esperar el MIN_REPEAT (que cuesta ~3 s de mas). Si algo
        # cambio (transicion/carga) no se toca, para no clickear encima de una pantalla que si avanzo.
        if (self.server_probe_until and time.time() < self.server_probe_until and not in_world
                and time.time() >= self.server_retry_at and self._server_stuck(gray)
                and self.cfg.get("server_early_retry", False)):
            rule = next(r for r in RULES if r["name"] == "server")
            self.info["phase"] = "server"
            self.next_delay = self.cfg["fast_poll"]
            if not online():
                self.info["phase"] = "offline"
                self.status("sin internet; espero a que vuelva")
                self.next_delay = 1.0
                return
            btn = find(gray, "start_btn", self.cfg["threshold"])
            if btn:
                self.status("Start no avanzó; reintento")
                self.do(hwnd, rule, btn, gray, 1)
                self.act_hist["server"] = [t for t in self.act_hist.get("server", [])
                                           if time.time() - t < 20] + [time.time()]
                self.last_act["server"] = time.time()
                self.info["acted"] += 1
                self.awake = True
                # re-armar por si este reintento tambien se pierde
                self.server_retry_at = time.time() + self.cfg.get("server_early_probe", 1.0)
                self.server_probe_until = time.time() + 6.0
                self._srv_base = cv2.resize(gray, (96, 54), interpolation=cv2.INTER_AREA).astype(np.int16)
                self.last_status = None
                time.sleep(self.cfg["action_cooldown"])
            return
        hit = detect(gray, in_world, self.cfg)
        if self.drop_start is not None and not in_world:
            self.drop_login_seen = True
        if hit and hit[0]["name"] == "disconnect" and self.drop_start is None:
            self.drop_start = time.time()
            self.drop_login_seen = False
            netmon.record_game_event("caida_juego")
        elif (not hit and in_world and self.drop_start is not None
              and (self.drop_login_seen or time.time() - self.drop_start > 120)):
            # (al cerrar el cartel el titulo sigue "en el mundo" un instante: no contarlo como reingreso)
            dur = time.time() - self.drop_start
            netmon.record_game_event("reingreso", dur)
            log.info("de vuelta en el mundo tras %s", netmon.fmt_dur(dur))
            self.drop_start = None
        if not hit and not in_world:
            self._record(bgr, "login")
        elif not hit and in_world and (ongoing >= 30 or self.ab_pending or self.drop_start is not None):
            self._record(bgr, "mundo")     # cartel de desconexion o avisos al entrar, tapados por un estilo de texto nuevo
        if hit or in_world:
            self.unk_n = 0
        elif not WIDE_SCALES:
            self.unk_n += 1
            if self.unk_n % 12 == 0:   # ~4 s sin reconocer nada: probar una vez con otras escalas por si la interfaz esta estirada
                global ACTIVE_SCALES
                keep, ACTIVE_SCALES = ACTIVE_SCALES, SCALES
                wide = detect(gray, in_world, self.cfg)
                ACTIVE_SCALES = keep
                if wide and abs(wide[1]["s"] - 1.0) > 1e-6:
                    globals()["WIDE_SCALES"] = True
                    log.info("La interfaz del juego aparece escalada (x%.2f): busco en varias escalas.", wide[1]["s"])
        if not hit:
            if in_world and self.ab_pending:
                self.next_delay = self.cfg["fast_poll"]
                self.handle_ab(hwnd)
                return
            if in_world:
                if self.awake and win32gui.GetForegroundWindow() != hwnd:
                    _sleep(hwnd)  # termino la secuencia: el juego vuelve a su estado inactivo normal
                self.awake = False
                if (self.restored_by_us and ongoing <= 0 and self.drop_start is None
                        and time.time() - self.peek_started >= self.peek_dwell):
                    win32gui.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
                    self.restored_by_us = False
                    log.info("todo en orden: vuelvo a minimizar el juego")
                self._exp_tick(bgr)
            else:
                self.next_delay = self.cfg["fast_poll"]  # cargando entre pantallas: mirar seguido
            self.status("en el mundo, todo normal" if in_world else "pantalla no reconocida (cargando?); espero")
            return
        rule, btn = hit
        name = rule["name"]
        self.next_delay = self.cfg["fast_poll"]
        if name == "server" and time.time() < self.server_hold_until:
            self.status("servidor caído: espero antes de volver a apretar Start")
            self.info["phase"] = "offline"
            self.next_delay = 2.0
            return
        if time.time() - self.last_act.get(name, 0) < MIN_REPEAT.get(name, 1.5):
            return  # recien clickeado: darle tiempo a que cambie la pantalla
        if not online():
            self.info["phase"] = "offline"
            self.status("sin internet; espero a que vuelva")
            self.next_delay = 1.0
            return
        self.info["phase"] = name
        self.status(f"detectado: {name}")
        if self.throttled(name):
            return
        now = time.time()
        recent = [t for t in self.act_hist.get(name, []) if now - t < 20]
        # En carteles y login el click corto nunca registro (en todos los relogs hacia falta el segundo intento):
        # se empieza por el largo, se reintenta con el largo y recien al tercer intento se usa el mouse real.
        level = 1 if len(recent) < 2 else 2
        self.do(hwnd, rule, btn, gray, level)
        if name == "connfail":
            self.connfail_n += 1
            wait = min(30 * 2 ** (self.connfail_n - 1), 120)    # 30 s, 60 s, 120 s, 120 s...
            self.server_hold_until = time.time() + wait
            if self.connfail_n in (1, 4):
                log.warning("El servidor no responde (¿mantenimiento?): reintento en %d s, y cada 2 min mientras siga caído.", wait)
        elif name in ("channel", "character"):
            self.connfail_n, self.server_hold_until = 0, 0.0   # el servidor volvio
        self.act_hist[name] = recent + [now]
        self.awake = True
        self.last_act[name] = time.time()
        self.info["acted"] += 1
        if name == "server":   # ventana de reintento rapido por si el click se pierde
            self.server_retry_at = time.time() + self.cfg.get("server_early_probe", 1.0)
            self.server_probe_until = time.time() + 6.0
            self._srv_base = cv2.resize(gray, (96, 54), interpolation=cv2.INTER_AREA).astype(np.int16)
        if name == "character" and self.cfg.get("angel_baby_after_relog", True):
            self.ab_pending, self.ab_tries, self.world_since = True, 0, None
        self.last_status = None
        time.sleep(self.cfg["action_cooldown"])


def analyze_image(path, cfg):
    img = cv2.imread(path)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    for in_world in (False, True):
        hit = detect(gray, in_world, cfg)
        print(f"  in_world={in_world}: ", end="")
        if hit:
            r, b = hit
            print(f"{r['name']}  score={b['score']:.2f} escala={b['s']} en ({b['x']},{b['y']})")
        else:
            print("nada")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="hacer clicks de verdad")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--image")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout), file_handler()])
    cfg = load_cfg()
    if a.live:
        cfg["dry_run"] = False
    if a.image:
        analyze_image(a.image, cfg)
        return
    bot = Relog(cfg)
    log.info("AutoRelog iniciado (%s, input=%s, canal=%d)",
             "ENSAYO" if cfg["dry_run"] else "EN VIVO", cfg["input"], bot.channel())
    try:
        while True:
            try:
                bot.step()
            except Exception:  # noqa: BLE001
                log.exception("error en el ciclo")
            if a.once:
                break
            time.sleep(bot.next_delay)
    except KeyboardInterrupt:
        log.info("detenido")


if __name__ == "__main__":
    main()
