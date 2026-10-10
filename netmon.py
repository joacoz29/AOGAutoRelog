"""Monitor de conexion a internet: registra cortes, duracion, latencia y arma el informe para el proveedor.

Todo se guarda en la carpeta datos/ (sobrevive a reinicios):
    cortes.jsonl     un corte por linea (inicio, fin, duracion, causa orientativa)
    latencia.csv     un resumen por minuto (pruebas, fallidas, latencia media y maxima)
    sesiones.jsonl   cuando estuvo abierto el programa (para calcular la cobertura de la medicion)
    juego.jsonl      desconexiones del juego y tiempo en volver al mundo
    estado.json      corte en curso y sesion actual (para recuperarse de un cierre inesperado)
"""
import bisect
import csv
import html
import json
import os
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "datos"
REPORTS = ROOT / "informes"
F_CUTS = DATA / "cortes.jsonl"
F_LAT = DATA / "latencia.csv"
F_SES = DATA / "sesiones.jsonl"
F_GAME = DATA / "juego.jsonl"
F_STATE = DATA / "estado.json"

TARGETS = [("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443)]   # Cloudflare, Google, Quad9
INTERVAL = 1.0      # segundos entre pruebas
TIMEOUT = 1.0       # espera maxima por servidor
CONFIRM = 2         # pruebas fallidas seguidas para declarar un corte
GAP = 20            # segundos sin medir a partir de los cuales se asume que la PC estuvo suspendida
NO_WINDOW = 0x08000000

CAUSAS = {
    "proveedor": "Internet (proveedor)",
    "red_local": "Red local (router / Wi-Fi / cable)",
    "desconocida": "No determinada",
}
PERIODS = [("hoy", "Hoy"), ("7d", "7 días"), ("30d", "30 días"), ("todo", "Todo")]


# ------------------------------------------------------------------ utilidades

def fmt_dur(sec):
    if sec is None:
        return "—"
    sec = int(round(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h} h {m:02d} min {s:02d} s"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


def fmt_ts(t, short=False):
    if t is None:
        return "—"
    return time.strftime("%d/%m %H:%M:%S" if short else "%d/%m/%Y %H:%M:%S", time.localtime(t))


def _write_json_atomic(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    os.replace(tmp, path)


def _append_jsonl(path, obj):
    DATA.mkdir(exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return out


def _read_state():
    try:
        return json.loads(F_STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


# ------------------------------------------------------------------ red

def default_gateway():
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric | Select-Object -First 1).NextHop"],
            capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
        gw = r.stdout.strip()
        return gw if gw and gw != "0.0.0.0" else None
    except Exception:  # noqa: BLE001
        return None


def ping(host, timeout_ms=700):
    try:
        r = subprocess.run(["ping", "-n", "1", "-w", str(timeout_ms), host], capture_output=True, text=True,
                           timeout=4, creationflags=NO_WINDOW)
        return "TTL=" in r.stdout.upper()  # una respuesta real trae TTL; 'host inaccesible' no
    except Exception:  # noqa: BLE001
        return False


def cause_of(o):
    """Causa orientativa a partir de los pings al router durante el corte."""
    if o.get("sin_gw"):
        return "red_local"          # ni siquiera hay puerta de enlace: la placa de red/Wi-Fi esta desconectada
    checks, fails = o.get("gw_checks", 0), o.get("gw_fails", 0)
    if not checks:
        return "desconocida"
    return "red_local" if fails / checks >= 0.5 else "proveedor"


def _tcp(target):
    t0 = time.perf_counter()
    try:
        socket.create_connection(target, TIMEOUT).close()
        return True, (time.perf_counter() - t0) * 1000
    except OSError:
        return False, None


# ------------------------------------------------------------------ monitor

class NetMonitor(threading.Thread):
    def __init__(self, on_msg=None, on_outage_end=None):
        super().__init__(daemon=True, name="netmon")
        self.on_msg = on_msg
        self.on_outage_end = on_outage_end
        self.stop_evt = threading.Event()
        self.lock = threading.Lock()
        self.online = None
        self.last_ms = None
        self.outage = None
        self.gateway = None
        self.session_start = None
        self._streak = 0
        self._first_fail = None
        self._acc = None
        self._pool = ThreadPoolExecutor(len(TARGETS))
        self._gw_checked = 0.0

    # -- helpers
    def _msg(self, text):
        if self.on_msg:
            try:
                self.on_msg(text)
            except Exception:  # noqa: BLE001
                pass

    def _save_state(self):
        try:
            _write_json_atomic(F_STATE, {"session": {"start": self.session_start, "last": time.time()},
                                         "outage": self.outage})
        except Exception:  # noqa: BLE001
            pass

    def current_minute(self):
        with self.lock:
            return dict(self._acc) if self._acc else None

    def snapshot(self):
        with self.lock:
            o = dict(self.outage) if self.outage else None
            return dict(online=self.online, ms=self.last_ms, outage=o, gateway=self.gateway,
                        session_start=self.session_start)

    # -- ciclo de vida
    def stop(self):
        self.stop_evt.set()

    def _recover_previous(self):
        """Cierra lo que haya quedado abierto si el programa se cerro de golpe la vez anterior."""
        st = _read_state()
        ses = st.get("session")
        if ses and ses.get("start"):
            _append_jsonl(F_SES, {"inicio": ses["start"], "fin": ses.get("last") or ses["start"], "cierre": "abrupto"})
        o = st.get("outage")
        if o and o.get("inicio"):
            end = (ses or {}).get("last") or o["inicio"]
            self._write_cut(o, end, incompleto=True)

    def _write_cut(self, o, end, incompleto=False):
        rec = dict(inicio=o["inicio"], fin=end, dur=round(end - o["inicio"], 1), causa=cause_of(o),
                   gateway=o.get("gw"), gw_checks=o.get("gw_checks", 0), gw_fails=o.get("gw_fails", 0),
                   incompleto=incompleto)
        _append_jsonl(F_CUTS, rec)
        return rec

    def run(self):
        DATA.mkdir(exist_ok=True)
        self._recover_previous()
        self.session_start = time.time()
        self.gateway = default_gateway()
        self._gw_checked = time.time()
        self._save_state()
        last_hb = time.time()
        last_end = time.time()
        try:
            while not self.stop_evt.is_set():
                t0 = time.time()
                if t0 - last_end > GAP:
                    self._suspended(last_end, t0)
                try:
                    self._cycle(t0)
                except Exception:  # noqa: BLE001
                    pass
                if time.time() - last_hb >= 10:
                    self._save_state()
                    last_hb = time.time()
                last_end = time.time()
                self.stop_evt.wait(max(0.05, INTERVAL - (time.time() - t0)))
                if self.gateway is None or time.time() - self._gw_checked > 300:
                    self._refresh_gateway_async()
        finally:
            self._flush_minute(force=True)
            now = time.time()
            if self.outage:  # se cierra el programa en medio de un corte: queda marcado como incompleto
                self._write_cut(self.outage, now, incompleto=True)
                self.outage = None
            _append_jsonl(F_SES, {"inicio": self.session_start, "fin": now, "cierre": "normal"})
            try:
                F_STATE.unlink()
            except OSError:
                pass

    def _suspended(self, last_end, now):
        """La PC estuvo suspendida/congelada entre last_end y now: cerrar la sesion ahi, sin inventar un corte."""
        _append_jsonl(F_SES, {"inicio": self.session_start, "fin": last_end, "cierre": "suspension"})
        self.session_start = now
        if self.outage:
            self._write_cut(self.outage, last_end, incompleto=True)
            with self.lock:
                self.outage = None
        self._streak = 0
        self._flush_minute()
        self._save_state()
        self._msg(f"La PC estuvo suspendida: sin medir de {fmt_ts(last_end).split(' ')[1]} a {fmt_ts(now).split(' ')[1]}")

    def _refresh_gateway_async(self):
        """El router se averigua en segundo plano (PowerShell tarda): nunca dentro del ciclo de medicion."""
        self._gw_checked = time.time()

        def work():
            gw = default_gateway()
            if gw:
                self.gateway = gw
        threading.Thread(target=work, daemon=True).start()

    # -- una prueba
    def _cycle(self, t0):
        res = list(self._pool.map(_tcp, TARGETS))
        oks = [ms for ok, ms in res if ok]
        ok = bool(oks)
        ms = min(oks) if oks else None
        with self.lock:
            self.online = ok
            if ok:
                self.last_ms = ms
        self._account(t0, ok, ms)
        if ok:
            if self.outage:
                self._end_outage(time.time())
            self._streak = 0
            return
        if self._streak == 0:
            self._first_fail = t0
        self._streak += 1
        if self._streak >= CONFIRM and self.outage is None:
            self._begin_outage(self._first_fail)
        elif self.outage and self._streak % 3 == 0:
            self._check_gateway()

    def _begin_outage(self, start):
        gw = self.gateway   # ya conocido (se refresca aparte): ni bien se corta no hay que demorar el ciclo con PowerShell
        o = dict(inicio=start, gw=gw, gw_checks=0, gw_fails=0, sin_gw=gw is None)
        if gw:
            self.gateway = gw
            o["gw_checks"] = 1
            o["gw_fails"] = 0 if ping(gw) else 1
        with self.lock:
            self.outage = o
        self._save_state()
        self._msg(f"Internet: se cortó a las {fmt_ts(start).split(' ')[1]}")

    def _check_gateway(self):
        o = self.outage
        if o and o.get("gw"):
            o["gw_checks"] += 1
            if not ping(o["gw"]):
                o["gw_fails"] += 1

    def _end_outage(self, end):
        o = self.outage
        rec = self._write_cut(o, end)
        with self.lock:
            self.outage = None
        self._save_state()
        self._msg(f"Internet: volvió tras {fmt_dur(rec['dur'])} — {CAUSAS[rec['causa']]}")
        if self.on_outage_end:
            try:
                self.on_outage_end(rec)
            except Exception:  # noqa: BLE001
                pass

    # -- resumen por minuto
    def _account(self, t, ok, ms):
        minute = int(t // 60) * 60
        if self._acc is None or self._acc["min"] != minute:
            self._flush_minute()
            with self.lock:
                self._acc = dict(min=minute, n=0, fail=0, sum=0.0, nok=0, max=0.0)
        with self.lock:
            a = self._acc
            a["n"] += 1
            if ok:
                a["nok"] += 1
                a["sum"] += ms
                a["max"] = max(a["max"], ms)
            else:
                a["fail"] += 1

    def _flush_minute(self, force=False):
        a = self._acc
        if not a:
            return
        try:
            new = not F_LAT.exists()
            with open(F_LAT, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["minuto_epoch", "pruebas", "fallidas", "lat_media_ms", "lat_max_ms"])
                w.writerow([a["min"], a["n"], a["fail"], round(a["sum"] / a["nok"], 1) if a["nok"] else "",
                            round(a["max"], 1) if a["nok"] else ""])
        except OSError:
            pass
        with self.lock:
            self._acc = None


# ------------------------------------------------------------------ eventos del juego

def record_game_event(kind, dur=None):
    """kind: 'caida_juego' (aparecio el cartel de conexion interrumpida) | 'reingreso' (volvio al mundo, dur = segundos)."""
    try:
        _append_jsonl(F_GAME, dict(t=time.time(), tipo=kind, dur=None if dur is None else round(dur, 1)))
    except OSError:
        pass


# ------------------------------------------------------------------ estadisticas

_lat_cache = {"path": None, "pos": 0, "rows": []}


def _lat_rows():
    """Filas (t, pruebas, fallidas, media, max) de latencia.csv. El archivo solo crece: se lee lo nuevo, no todo."""
    c = _lat_cache
    if c["path"] != str(F_LAT):
        c.update(path=str(F_LAT), pos=0, rows=[])
    try:
        size = F_LAT.stat().st_size
    except OSError:
        return c["rows"]
    if size < c["pos"]:
        c.update(pos=0, rows=[])
    if size > c["pos"]:
        with open(F_LAT, "rb") as f:
            f.seek(c["pos"])
            data = f.read()
        end = data.rfind(b"\n")
        if end >= 0:
            for line in data[:end + 1].decode("utf-8", "replace").splitlines():
                p = line.split(",")
                try:
                    c["rows"].append((float(p[0]), int(p[1]), int(p[2]),
                                      float(p[3]) if p[3] else None, float(p[4]) if p[4] else None))
                except (ValueError, IndexError):
                    continue  # encabezado o linea incompleta
            c["pos"] += end + 1
    return c["rows"]


def period_bounds(key, now=None):
    now = now or time.time()
    if key == "hoy":
        lt = time.localtime(now)
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)), now
    if key == "7d":
        return now - 7 * 86400, now
    if key == "30d":
        return now - 30 * 86400, now
    starts = [s["inicio"] for s in _read_jsonl(F_SES)] + [c["inicio"] for c in _read_jsonl(F_CUTS)]
    st = _read_state().get("session", {}).get("start")
    if st:
        starts.append(st)
    return (min(starts) if starts else now - 3600), now


def _overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def sessions_list(now=None, mon=None):
    now = now or time.time()
    ses = [(s["inicio"], s["fin"]) for s in _read_jsonl(F_SES)]
    start = mon.session_start if mon and mon.session_start else _read_state().get("session", {}).get("start")
    if start:
        ses.append((start, now))
    return ses


def cuts_list(now=None, mon=None):
    """Cortes terminados + el que esta en curso (con fin=now y en_curso=True)."""
    now = now or time.time()
    cuts = _read_jsonl(F_CUTS)
    if mon:
        o = mon.snapshot()["outage"]
        if o:
            cuts.append(dict(inicio=o["inicio"], fin=now, dur=round(now - o["inicio"], 1), causa=cause_of(o),
                             gateway=o.get("gw"), en_curso=True, incompleto=False))
    return cuts


def stats(key, mon=None, now=None):
    now = now or time.time()
    p0, p1 = period_bounds(key, now)
    span = max(1.0, p1 - p0)
    cuts = [c for c in cuts_list(now, mon) if _overlap(c["inicio"], c["fin"], p0, p1) > 0]
    durs = [min(c["fin"], p1) - max(c["inicio"], p0) for c in cuts]
    down = sum(durs)
    monitored = sum(_overlap(a, b, p0, p1) for a, b in sessions_list(now, mon))
    n = fail = nok = 0
    wsum = lmax = 0.0
    rows = _lat_rows()
    lo = bisect.bisect_left(rows, (p0,))
    hi = bisect.bisect_right(rows, (p1, float("inf")))
    for t, pr, fa, avg, mx in rows[lo:hi]:
        n += pr
        fail += fa
        if avg is not None:
            k = pr - fa
            nok += k
            wsum += avg * k
            lmax = max(lmax, mx or 0.0)
    a = mon.current_minute() if mon else None   # el minuto en curso todavia no esta en el CSV
    if a and p0 <= a["min"] <= p1:
        n += a["n"]
        fail += a["fail"]
        if a["nok"]:
            nok += a["nok"]
            wsum += a["sum"]
            lmax = max(lmax, a["max"])
    g = [e for e in _read_jsonl(F_GAME) if p0 <= e["t"] <= p1]
    reentries = [e["dur"] for e in g if e["tipo"] == "reingreso" and e.get("dur") is not None]
    return dict(
        key=key, p0=p0, p1=p1, cuts=sorted(cuts, key=lambda c: c["inicio"], reverse=True),
        count=len(cuts), down=down, longest=max(durs) if durs else None,
        avg=(down / len(cuts)) if cuts else None,
        n_prov=sum(1 for c in cuts if c["causa"] == "proveedor"), n_local=sum(1 for c in cuts if c["causa"] == "red_local"),
        monitored=monitored, coverage=min(100.0, 100 * monitored / span),
        availability=(100 * (1 - min(down, monitored) / monitored)) if monitored > 0 else None,
        probes=n, failed=fail, loss=(100 * fail / n) if n else None,
        lat_avg=(wsum / nok) if nok else None, lat_max=lmax if nok else None,
        game_drops=sum(1 for e in g if e["tipo"] == "caida_juego"), reentry_avg=(sum(reentries) / len(reentries)) if reentries else None,
        reentries=reentries, game_events=g,
    )


def period_label(st):
    return {"hoy": "Hoy", "7d": "Últimos 7 días", "30d": "Últimos 30 días", "todo": "Todo el historial"}[st["key"]]


# ------------------------------------------------------------------ informes

def summary_text(st, isp=""):
    L = [f"Informe de conexión a internet{' — ' + isp if isp else ''}",
         f"Período: {period_label(st)} ({fmt_ts(st['p0'])} a {fmt_ts(st['p1'])})",
         f"Medición: {st['coverage']:.1f}% del período ({fmt_dur(st['monitored'])} medidos)",
         f"Cortes de internet: {st['count']}  (proveedor: {st['n_prov']}, red local: {st['n_local']})",
         f"Tiempo total sin internet: {fmt_dur(st['down'])}",
         f"Corte más largo: {fmt_dur(st['longest'])}   Duración promedio: {fmt_dur(st['avg'])}"]
    if st["availability"] is not None:
        L.append(f"Disponibilidad: {st['availability']:.3f}%")
    if st["loss"] is not None:
        L.append(f"Pruebas fallidas: {st['loss']:.2f}% ({st['failed']} de {st['probes']})")
    if st["lat_avg"] is not None:
        L.append(f"Latencia promedio: {st['lat_avg']:.0f} ms (máxima {st['lat_max']:.0f} ms)")
    L.append(f"Desconexiones del juego: {st['game_drops']}")
    L.append("")
    L.append("Detalle de cortes (más reciente primero):")
    for c in st["cuts"][:60]:
        fin = "en curso" if c.get("en_curso") else fmt_ts(c["fin"])
        L.append(f"  {fmt_ts(c['inicio'])} -> {fin}   {fmt_dur(c['dur'])}   {CAUSAS[c['causa']]}")
    return "\n".join(L)


def export_report(st, isp=""):
    """Escribe informes/informe_conexion_<fecha>.html y .csv. Devuelve la ruta del HTML."""
    REPORTS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    html_path = REPORTS / f"informe_conexion_{stamp}.html"
    csv_path = REPORTS / f"cortes_{stamp}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["inicio", "fin", "duracion_segundos", "duracion", "causa_orientativa", "router", "en_curso", "medicion_incompleta"])
        for c in sorted(st["cuts"], key=lambda c: c["inicio"]):
            w.writerow([fmt_ts(c["inicio"]), "" if c.get("en_curso") else fmt_ts(c["fin"]), round(c["dur"]), fmt_dur(c["dur"]),
                        CAUSAS[c["causa"]], c.get("gateway") or "", "si" if c.get("en_curso") else "no",
                        "si" if c.get("incompleto") else "no"])
    e = html.escape

    def kv(k, v):
        return f"<tr><th>{e(k)}</th><td>{e(str(v))}</td></tr>"

    rows = "".join(
        f"<tr><td>{fmt_ts(c['inicio'])}</td><td>{'en curso' if c.get('en_curso') else fmt_ts(c['fin'])}</td>"
        f"<td>{fmt_dur(c['dur'])}</td><td>{e(CAUSAS[c['causa']])}</td></tr>"
        for c in sorted(st["cuts"], key=lambda c: c["inicio"])) or "<tr><td colspan='4'>Sin cortes en el período.</td></tr>"
    game = "".join(f"<tr><td>{fmt_ts(g['t'])}</td><td>{'Cartel «conexión interrumpida»' if g['tipo'] == 'caida_juego' else 'Volvió al mundo (' + fmt_dur(g.get('dur')) + ')'}</td></tr>"
                   for g in st["game_events"]) or "<tr><td colspan='2'>Sin eventos del juego.</td></tr>"
    doc = f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Informe de conexión</title>
<style>body{{font:15px/1.5 Segoe UI,Arial,sans-serif;max-width:860px;margin:32px auto;padding:0 16px;color:#1b2a33}}
h1{{font-size:24px;margin:0 0 4px}}h2{{font-size:17px;margin:28px 0 8px;border-bottom:1px solid #cfd8dc;padding-bottom:4px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #cfd8dc;padding:6px 10px;text-align:left;font-size:14px}}
th{{background:#eef3f5;width:34%}}.t th{{width:auto}}.m{{color:#5b6b75;font-size:13px}}@media print{{body{{margin:0}}}}</style></head><body>
<h1>Informe de estabilidad de conexión a internet</h1>
<div class="m">{('Proveedor: ' + e(isp) + ' · ') if isp else ''}Equipo: {e(socket.gethostname())} · Generado: {fmt_ts(time.time())}</div>
<h2>Resumen — {e(period_label(st))}</h2>
<table>{kv('Período', f"{fmt_ts(st['p0'])} a {fmt_ts(st['p1'])}")}
{kv('Cobertura de la medición', f"{st['coverage']:.1f}% del período ({fmt_dur(st['monitored'])} medidos)")}
{kv('Cantidad de cortes', st['count'])}
{kv('  de los cuales, con el router respondiendo (problema del proveedor)', st['n_prov'])}
{kv('  de los cuales, con el router sin responder (red local)', st['n_local'])}
{kv('Tiempo total sin internet', fmt_dur(st['down']))}
{kv('Corte más largo', fmt_dur(st['longest']))}
{kv('Duración promedio de un corte', fmt_dur(st['avg']))}
{kv('Disponibilidad', '—' if st['availability'] is None else f"{st['availability']:.3f}%")}
{kv('Pruebas fallidas', '—' if st['loss'] is None else f"{st['loss']:.2f}% ({st['failed']} de {st['probes']})")}
{kv('Latencia promedio / máxima', '—' if st['lat_avg'] is None else f"{st['lat_avg']:.0f} ms / {st['lat_max']:.0f} ms")}
{kv('Desconexiones del juego asociadas', st['game_drops'])}</table>
<h2>Detalle de cortes</h2>
<table class="t"><tr><th>Inicio</th><th>Fin</th><th>Duración</th><th>Causa orientativa</th></tr>{rows}</table>
<h2>Eventos del juego (Angels Online)</h2>
<table class="t"><tr><th>Hora</th><th>Evento</th></tr>{game}</table>
<h2>Cómo se midió</h2>
<p class="m">Una prueba por segundo: conexión TCP a tres servidores públicos independientes (1.1.1.1:443 Cloudflare, 8.8.8.8:53 Google y 9.9.9.9:443 Quad9).
Un corte se declara cuando fallan los tres servidores durante al menos {CONFIRM} pruebas seguidas (≈{CONFIRM} s) y su inicio se registra a la hora de la primera prueba fallida;
termina con la primera prueba exitosa. Durante cada corte se hace ping al router (puerta de enlace): si responde, la falla es posterior al router (proveedor);
si no responde, el problema está en la red local (Wi-Fi, cable o router). El diagnóstico de causa es <b>orientativo</b>. Solo hay datos mientras el programa está abierto; la cobertura indica qué parte del período se midió.
Las mediciones se hicieron desde un único equipo.</p></body></html>"""
    html_path.write_text(doc, encoding="utf-8")
    return html_path
