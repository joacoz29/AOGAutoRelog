"""Experiencia: lectura del porcentaje del HUD y estadisticas de ritmo (exp por hora, sesion, tiempo al proximo nivel).

El juego dibuja "NN.NN %" con una tipografia de pixeles (blanca sobre celdas negras, la misma de HP y MP).
Se aisla la tinta (brillo medio/alto y saturacion baja, asi no depende del color de la barra), se separa cada
caracter y se clasifica contra prototipos guardados en templates/exp_digits.npz (tolerando 1 px de desplazamiento).

Los datos se guardan en datos/exp.csv: t, pct, ganancia, tipo   (tipo: gain | lvl | loss | hb)
"""
import csv
import threading
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).parent
DATA = ROOT / "datos"
F_EXP = DATA / "exp.csv"
DIGITS = ROOT / "templates" / "exp_digits.npz"

BAND = (19, 28, 150, 196)      # y0, y1, x0, x1 del texto "NN.NN" en el cliente de 1366x768 (se usa para entrenar los digitos)
ROI = (4, 44, 100, 320)        # zona del HUD donde se busca el texto al calibrar: y0, y1, x0, x1
TEXT_H = 10                    # alto de la banda de lectura (filas)
PH, PW = 10, 7                 # tamano del lienzo en que se comparan los caracteres
MAX_DIST = 14                  # distancia maxima (pixeles distintos) para aceptar un caracter
GAP = 300                      # s: mas que esto entre lecturas = el personaje no estaba jugando (no cuenta como tiempo activo)
NOISE = 0.30                   # una baja menor a esto (en %) se toma como error de lectura, no como perdida de exp
LEVEL_WRAP = 50                # una baja mayor a esto (en %) es una subida de nivel (la barra vuelve a ~0)
HB = 60                        # s entre latidos (filas sin cambio) para poder medir el tiempo activo


# ------------------------------------------------------------------ lectura

def ink(img_bgr):
    """Tinta del texto: brillo >= 100 y saturacion <= 70 (el fondo de la barra es de color; el texto es gris/blanco)."""
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    return (hsv[..., 2] >= 100) & (hsv[..., 1] <= 70)


def column_runs(b):
    cols = np.where(b.any(axis=0))[0]
    if len(cols) == 0:
        return []
    out, s, p = [], cols[0], cols[0]
    for c in cols[1:]:
        if c != p + 1:
            out.append((s, p))
            s = c
        p = c
    out.append((s, p))
    return out


def glyphs(img_bgr, band=BAND):
    """Lista de mascaras booleanas, una por caracter, recortadas a su caja."""
    y0, y1, x0, x1 = band
    b = ink(img_bgr)[y0:y1 + 1, x0:x1 + 1]
    out = []
    for a, z in column_runs(b):
        sub = b[:, a:z + 1]
        ys = np.where(sub.any(axis=1))[0]
        out.append(sub[ys[0]:ys[-1] + 1].copy())
    return out


def pad(m):
    o = np.zeros((PH + 2, PW + 2), np.float32)
    h, w = m.shape
    o[1:1 + min(h, PH), 1:1 + min(w, PW)] = m[:PH, :PW]
    return o


class DigitReader:
    def __init__(self, path=DIGITS):
        z = np.load(path)
        self.labels = [str(x) for x in z["labels"]]
        self.protos = z["masks"].astype(np.float32)          # (N, PH+2, PW+2)
        self.bands = {}                                      # (alto, ancho) de la captura -> (fila, columna) del texto
        self.fails = 0
        self.last_try = 0.0

    def classify(self, m):
        """(etiqueta, distancia, margen sobre la 2da mejor etiqueta distinta)."""
        c = pad(m)
        best = np.full(len(self.protos), 1e9)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                s = np.roll(np.roll(c, dy, 0), dx, 1)
                best = np.minimum(best, ((self.protos - s) ** 2).sum(axis=(1, 2)))
        per = {}
        for lab, d in zip(self.labels, best):
            per[lab] = min(per.get(lab, 1e9), float(d))
        ranked = sorted((d, lab) for lab, d in per.items())
        margin = ranked[1][0] - ranked[0][0] if len(ranked) > 1 else 99.0
        return ranked[0][1], ranked[0][0], margin

    # -- lectura de una secuencia de caracteres ya separados: "d.dd" o "dd.dd" (el resto, p. ej. el '%', se ignora)
    def _parse(self, masks):
        """(valor, suma de distancias) o None."""
        text, dist, frac = "", 0.0, None
        for m in masks:
            if m.shape[0] <= 3 and m.shape[1] <= 3:           # el punto decimal: un cuadradito abajo
                if frac is not None or not text:
                    return None
                text, frac = text + ".", 0
                continue
            if not (6 <= m.shape[0] <= 9 and 1 <= m.shape[1] <= 6):   # no tiene forma de digito
                return None
            lab, d, _ = self.classify(m)
            if d > MAX_DIST:
                return None
            text, dist = text + lab, dist + d
            if frac is not None:
                frac += 1
                if frac == 2:
                    break
            elif len(text) > 2:
                return None
        if frac != 2:
            return None
        v = float(text)
        return (v, dist) if 0 <= v < 100 else None

    def _masks(self, ink_mask, y0, x_start, limit=8):
        """Caracteres de la banda y0..y0+TEXT_H desde la columna x_start, como mascaras recortadas."""
        b = ink_mask[y0:y0 + TEXT_H, x_start:x_start + 110]
        out = []
        for a, z in column_runs(b)[:limit]:
            sub = b[:, a:z + 1]
            ys = np.where(sub.any(axis=1))[0]
            out.append(sub[ys[0]:ys[-1] + 1].copy())
        return out

    def calibrate(self, img_bgr):
        """Busca en la zona del HUD donde esta el texto de la experiencia. Devuelve ((y0, x_start), valor) o None."""
        y0r, y1r, x0r, x1r = ROI
        ink_mask = ink(img_bgr)
        best = None
        for y0 in range(y0r, y1r):
            region = ink_mask[y0:y0 + TEXT_H, x0r:x1r]
            for a, z in column_runs(region):
                if not 1 <= z - a + 1 <= 6:
                    continue
                x_start = x0r + a
                res = self._parse(self._masks(ink_mask, y0, x_start))
                if res and (best is None or res[1] < best[0]):
                    best = (res[1], (y0, x_start), res[0])
        return (best[1], best[2]) if best else None

    def read_at(self, img_bgr, band):
        y0, x_start = band
        res = self._parse(self._masks(ink(img_bgr), y0, x_start))
        return res[0] if res else None

    def read_pct(self, img_bgr):
        """Porcentaje de experiencia (float) o None. El HUD cambia de lugar con la resolucion: se calibra por tamano de ventana
        (y se vuelve a calibrar si la posicion recordada deja de funcionar 3 veces seguidas)."""
        key = img_bgr.shape[:2]
        band = self.bands.get(key)
        if band:
            v = self.read_at(img_bgr, band)
            if v is not None:
                self.fails = 0
                return v
            self.fails += 1
            if self.fails < 3:
                return None
            del self.bands[key]
            self.fails = 0
        if time.time() - self.last_try < 30:                   # no recalibrar a cada lectura si el HUD no esta a la vista
            return None
        self.last_try = time.time()
        found = self.calibrate(img_bgr)
        if not found:
            return None
        self.bands[key] = found[0]
        return found[1]


# ------------------------------------------------------------------ estadisticas

class ExpTracker:
    def __init__(self, path=None):
        self.path = Path(path) if path else F_EXP
        self.lock = threading.Lock()
        self.rows = []                       # (t, pct, ganancia, tipo)
        self.last_pct = None
        self.last_t = 0.0
        self.last_gain_t = None
        self.session_start = time.time()
        self.session_idx = 0                 # a partir de que fila cuenta la sesion actual
        self._pending = None                 # lectura sospechosa esperando confirmacion
        self._stall_notified = False
        self._load()

    def _load(self):
        try:
            with open(self.path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    try:
                        self.rows.append((float(r["t"]), float(r["pct"]), float(r["ganancia"]), r["tipo"]))
                    except (KeyError, ValueError):
                        continue
        except FileNotFoundError:
            pass
        if self.rows:
            self.last_t, self.last_pct = self.rows[-1][0], self.rows[-1][1]
            gains = [r[0] for r in self.rows if r[3] in ("gain", "lvl")]
            self.last_gain_t = gains[-1] if gains else None
        self.session_idx = len(self.rows)

    def _write(self, row):
        self.rows.append(row)
        try:
            self.path.parent.mkdir(exist_ok=True)
            new = not self.path.exists()
            with open(self.path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["t", "pct", "ganancia", "tipo"])
                w.writerow([round(row[0], 1), row[1], round(row[2], 3), row[3]])
        except OSError:
            pass

    def reset_session(self):
        with self.lock:
            self.session_start = time.time()
            self.session_idx = len(self.rows)

    def feed(self, pct, t=None):
        """Registra una lectura. Devuelve 'gain' | 'lvl' | 'loss' | 'same' | 'noise' | 'pending' | 'first'."""
        t = t or time.time()
        with self.lock:
            prev = self.last_pct
            if prev is None or t - self.last_t > 3600:          # primera lectura (o despues de mucho tiempo)
                self.last_pct, self.last_t = pct, t
                self._write((t, pct, 0.0, "hb"))
                return "first"
            d = pct - prev
            if abs(d) < 1e-9:
                if t - self.last_t >= HB:
                    self._write((t, pct, 0.0, "hb"))
                    self.last_t = t
                self._pending = None
                return "same"
            if -NOISE <= d < 0:                                  # error de lectura de un digito: ignorar
                return "noise"
            big = d < -LEVEL_WRAP or d > 10                      # salto sospechoso: pedir una segunda lectura igual
            if big and (self._pending is None or abs(self._pending - pct) > 1e-9):
                self._pending = pct
                return "pending"
            self._pending = None
            if d >= 0:
                kind, gain = "gain", d
            elif d < -LEVEL_WRAP:
                kind, gain = "lvl", (100 - prev) + pct          # subio de nivel: la barra volvio a empezar
            else:
                kind, gain = "loss", d                          # bajo de verdad (p. ej. murio y perdio exp)
            self.last_pct, self.last_t = pct, t
            if kind in ("gain", "lvl"):
                self.last_gain_t = t
                self._stall_notified = False
            self._write((t, pct, gain, kind))
            return kind

    # -- consultas
    def _window(self, since, now):
        rows = [r for r in self.rows if r[0] >= since]
        return rows

    def stats(self, since=None, now=None):
        """since = None -> sesion actual. Devuelve ganancia, perdida, subidas, tiempo activo y ritmo por hora."""
        now = now or time.time()
        with self.lock:
            rows = self.rows[self.session_idx:] if since is None else self._window(since, now)
            gained = sum(r[2] for r in rows if r[3] in ("gain", "lvl"))
            lost = -sum(r[2] for r in rows if r[3] == "loss")
            lvls = sum(1 for r in rows if r[3] == "lvl")
            active = 0.0
            for a, b in zip(rows, rows[1:]):
                dt = b[0] - a[0]
                if dt <= GAP:
                    active += dt
            rate = (gained / active * 3600) if active >= 120 else None
            return dict(gained=gained, lost=lost, levels=lvls, active=active, rate=rate, n=len(rows),
                        pct=self.last_pct, last_t=self.last_t)

    def buckets(self, n=18, width=600, now=None):
        """Ganancia por tramos de `width` s, de mas viejo a mas nuevo."""
        now = now or time.time()
        start = now - n * width
        out = [0.0] * n
        with self.lock:
            for t, _, g, k in self.rows:
                if t >= start and k in ("gain", "lvl"):
                    out[min(n - 1, int((t - start) // width))] += g
        return out

    def eta_next_level(self, now=None):
        """Segundos hasta el proximo nivel con el ritmo de los ultimos 30 min (o el de la sesion). None si no se puede."""
        now = now or time.time()
        s = self.stats(now - 1800, now)
        rate = s["rate"] or self.stats(None, now)["rate"]
        if not rate or rate <= 0 or self.last_pct is None:
            return None
        return (100 - self.last_pct) / rate * 3600

    def stalled_for(self, now=None):
        """Segundos sin ganar exp, solo si se esta leyendo (ultima lectura reciente)."""
        now = now or time.time()
        if self.last_pct is None or now - self.last_t > 180:
            return None
        ref = self.last_gain_t or self.session_start
        return max(0.0, now - ref)

    def should_notify_stall(self, minutes, now=None):
        s = self.stalled_for(now)
        if s is not None and s >= minutes * 60 and not self._stall_notified:
            self._stall_notified = True
            return s
        return None


def fmt_dur(sec):
    if sec is None:
        return "—"
    sec = int(round(sec))
    h, r = divmod(sec, 3600)
    m = r // 60
    return f"{h} h {m:02d} min" if h else f"{m} min"
