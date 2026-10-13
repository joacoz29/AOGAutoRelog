"""Interfaz de AutoRelog (estilo BetterAngels): oscura, acento verde agua, clientes y actividad a la derecha."""
import json
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import autorelog as core
import expmon
import netmon
import tray

VERSION = "0.1"
BG, PANEL, PANEL2, LINE = "#0b1218", "#0f1a22", "#13212b", "#1f3340"
FG, MUTED, ACCENT, WARN, DANGER = "#d7e3ea", "#6f8794", "#6FCABF", "#e6b450", "#e5675f"
F_UI = ("Segoe UI", 10)
F_SM = ("Segoe UI", 9)
F_MONO = ("Consolas", 9)
F_BRAND = ("Georgia", 17, "bold")
F_H1 = ("Georgia", 22)

STEPS = [("disconnect", "Desconexión"), ("server", "Servidor"), ("channel", "Canal"),
         ("character", "Personaje"), ("world", "En el mundo"), ("angelbaby", "Angel Baby")]
PHASE_TO_STEP = {"disconnect": 0, "server": 1, "channel": 2, "announcement": 3, "pin": 3,
                 "character": 3, "tradepin": 4, "world": 4, "angelbaby": 5}
PHASE_TEXT = {
    "idle": "Detenido", "nowindow": "Sin ventana del juego", "minimized": "Juego minimizado (no lo puedo ver)",
    "angelbaby": "Activando el Angel Baby", "unknown": "Cargando / pantalla no reconocida",
    "world": "En el mundo", "offline": "Sin internet, esperando", "disconnect": "Desconexión detectada",
    "server": "Eligiendo servidor", "channel": "Eligiendo canal", "character": "Entrando al juego",
    "announcement": "Cerrando anuncio", "pin": "Cerrando aviso de PIN", "tradepin": "Cerrando aviso de Trade PIN",
}


class QueueHandler(logging.Handler):
    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put((time.strftime("%H:%M:%S"), record.getMessage(), record.levelno))


class Btn(tk.Label):
    def __init__(self, parent, text, command, primary=False, **kw):
        self.primary = primary
        super().__init__(parent, text=text, font=F_UI, cursor="hand2", padx=16, pady=7, bd=0, **kw)
        self.command = command
        self.normal = (ACCENT, BG) if primary else (PANEL2, FG)
        self.hover = ("#8be0d4", BG) if primary else (LINE, FG)
        self.apply(self.normal)
        self.bind("<Enter>", lambda e: self.apply(self.hover))
        self.bind("<Leave>", lambda e: self.apply(self.normal))
        self.bind("<Button-1>", lambda e: self.command())

    def apply(self, c):
        self.config(bg=c[0], fg=c[1], highlightthickness=1, highlightbackground=ACCENT if self.primary else LINE)

    def set(self, text, primary=None):
        if primary is not None:
            self.primary = primary
            self.normal = (ACCENT, BG) if primary else (PANEL2, FG)
            self.hover = ("#8be0d4", BG) if primary else (LINE, FG)
        self.config(text=text)
        self.apply(self.normal)


class Segmented(tk.Frame):
    """Selector tipo 'off | live'."""

    def __init__(self, parent, options, value, on_change, font=F_SM):
        super().__init__(parent, bg=PANEL2, highlightthickness=1, highlightbackground=LINE)
        self.on_change, self.labels, self.value = on_change, {}, value
        for key, text in options:
            lb = tk.Label(self, text=text, font=font, padx=11, pady=4, cursor="hand2")
            lb.pack(side="left")
            lb.bind("<Button-1>", lambda e, k=key: self.select(k, True))
            self.labels[key] = lb
        self.select(value, False)

    def select(self, key, notify):
        self.value = key
        for k, lb in self.labels.items():
            lb.config(bg=PANEL if k == key else PANEL2, fg=ACCENT if k == key else MUTED)
        if notify:
            self.on_change(key)


def card(parent, **kw):
    return tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=LINE, **kw)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AutoRelog")
        self.geometry("1260x780")
        self.minsize(1180, 700)
        self.configure(bg=BG)
        self.cfg = core.load_cfg()
        self.bot = core.Relog(self.cfg)
        self.bot.outage_probe = self._outage_secs
        self.running = False
        self.stop_evt = threading.Event()
        self.q = queue.Queue()
        self.n_events = 0

        root = logging.getLogger("autorelog")
        root.setLevel(logging.INFO)
        root.addHandler(QueueHandler(self.q))
        root.addHandler(core.file_handler())

        self.style_widgets()
        self.net = netmon.NetMonitor(on_msg=lambda m: core.log.info(m),   # mide internet siempre que la app este abierta
                                     on_outage_end=self.on_outage_end)
        self.net.start()
        self.current_page = "main"
        self._net_sig = None

        self.tray_q = queue.Queue()
        self.tray = tray.Tray(self.tray_q.put, lambda: {"running": self.running})
        self.tray.start()
        self._hint_shown = False
        try:
            self.iconbitmap(str(tray.icon_path("ok")))
        except Exception:  # noqa: BLE001
            pass

        self.build_header()
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        self.build_sidebar(body)
        self.pages = tk.Frame(body, bg=BG)
        self.pages.grid(row=0, column=1, sticky="nsew", padx=(0, 0))
        self.pages.columnconfigure(0, weight=1)
        self.pages.rowconfigure(0, weight=1)
        self.page_main = self.build_main(self.pages)
        self.page_cfg = self.build_settings(self.pages)
        self.page_net = self.build_net(self.pages)
        self.page_exp = self.build_exp(self.pages)
        self.build_right(body)
        self.show("main")
        self.update_sub()
        if self.cfg.get("autostart", True):
            self.after(800, self.toggle_run)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        if self.cfg.get("start_in_tray", False):
            self.after(100, self.withdraw)
        self.after(250, self.tick)
        self.after(1500, self.refresh_client)
        self.after(600, self.refresh_net_header)
        self.after(900, self.refresh_net)
        self.after(1100, self.refresh_exp)

    # ------------------------------------------------------------ construccion
    def style_widgets(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure("Net.Treeview", background=PANEL2, fieldbackground=PANEL2, foreground=FG, rowheight=26,
                     borderwidth=0, font=F_SM)
        st.layout("Net.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        st.configure("Net.Treeview.Heading", background=PANEL, foreground=MUTED, relief="flat", borderwidth=0,
                     font=("Segoe UI", 9, "bold"))
        st.map("Net.Treeview", background=[("selected", LINE)], foreground=[("selected", FG)])
        st.map("Net.Treeview.Heading", background=[("active", PANEL)])
        st.configure("Dark.Vertical.TScrollbar", background=LINE, troughcolor=PANEL, bordercolor=PANEL,
                     arrowcolor=MUTED, lightcolor=LINE, darkcolor=LINE, gripcount=0)
        st.map("Dark.Vertical.TScrollbar",
               background=[("disabled", PANEL), ("active", "#2c4a5c"), ("pressed", "#2c4a5c")],
               arrowcolor=[("disabled", LINE), ("active", FG)],
               bordercolor=[("disabled", PANEL)], lightcolor=[("disabled", PANEL)], darkcolor=[("disabled", PANEL)])

    def build_header(self):
        h = tk.Frame(self, bg=PANEL, height=56, highlightthickness=1, highlightbackground=LINE)
        h.pack(fill="x")
        h.pack_propagate(False)
        tk.Label(h, text="AutoRelog", font=F_BRAND, bg=PANEL, fg=FG).pack(side="left", padx=(18, 8))
        tk.Label(h, text=f"v{VERSION}", font=F_MONO, bg=PANEL, fg=MUTED).pack(side="left", pady=(6, 0))
        self.mode = Segmented(h, [("ensayo", "ensayo"), ("live", "live")],
                              "ensayo" if self.cfg["dry_run"] else "live", self.on_mode, font=F_MONO)
        self.mode.pack(side="right", padx=18)
        tk.Label(h, text="escritura:", font=F_SM, bg=PANEL, fg=MUTED).pack(side="right")
        self.net_lbl = tk.Label(h, text="● Internet · midiendo…", font=F_MONO, bg=PANEL, fg=MUTED)
        self.net_lbl.pack(side="right", padx=(0, 26))

    def build_sidebar(self, body):
        sb = tk.Frame(body, bg=PANEL, width=210, highlightthickness=1, highlightbackground=LINE)
        sb.grid(row=0, column=0, sticky="ns")
        sb.pack_propagate(False)
        self.nav = {}
        tk.Label(sb, text="CUENTA", font=("Consolas", 8, "bold"), bg=PANEL, fg=MUTED).pack(anchor="w", padx=18, pady=(18, 6))
        for key, text in (("main", "Auto Relog"), ("exp", "Experiencia"), ("net", "Conexión"), ("cfg", "Ajustes")):
            lb = tk.Label(sb, text="  " + text, font=F_UI, anchor="w", bg=PANEL, fg=FG, padx=10, pady=8, cursor="hand2")
            lb.pack(fill="x", padx=8)
            lb.bind("<Button-1>", lambda e, k=key: self.show(k))
            self.nav[key] = lb
        tk.Label(sb, text="Hecho a medida para\nAngels Online Global", font=F_SM, bg=PANEL, fg=MUTED,
                 justify="left").pack(side="bottom", anchor="w", padx=18, pady=16)

    def build_main(self, parent):
        p = tk.Frame(parent, bg=BG)
        p.grid(row=0, column=0, sticky="nsew")
        tk.Label(p, text="Auto Relog", font=F_H1, bg=BG, fg=FG).pack(anchor="w", padx=28, pady=(24, 4))
        tk.Label(p, text="Si se corta la conexión, vuelve a entrar solo: cierra el aviso, elige servidor y canal "
                 "y entra al juego.\nNo guarda contraseñas: usa la sesión que el juego ya tiene abierta.",
                 font=F_UI, bg=BG, fg=MUTED, justify="left", wraplength=520).pack(anchor="w", padx=28)

        st = card(p)
        st.pack(fill="x", padx=28, pady=18)
        top = tk.Frame(st, bg=PANEL)
        top.pack(fill="x", padx=18, pady=16)
        self.dot = tk.Label(top, text="●", font=("Segoe UI", 16), bg=PANEL, fg=MUTED)
        self.dot.pack(side="left")
        col = tk.Frame(top, bg=PANEL)
        col.pack(side="left", padx=10)
        self.state_lbl = tk.Label(col, text="Detenido", font=("Segoe UI", 13, "bold"), bg=PANEL, fg=FG, anchor="w")
        self.state_lbl.pack(anchor="w")
        self.sub_lbl = tk.Label(col, text="Modo ensayo: solo anota lo que haría", font=F_SM, bg=PANEL, fg=MUTED)
        self.sub_lbl.pack(anchor="w")
        self.start_btn = Btn(top, "Iniciar", self.toggle_run, primary=True)
        self.start_btn.pack(side="right")
        Btn(top, "Capturar", lambda: threading.Thread(target=self.bot.save_frame, daemon=True).start()).pack(side="right", padx=(8, 0))
        Btn(top, "Angel Baby", self.test_ab).pack(side="right", padx=(8, 0))
        Btn(top, "Probar", self.test_once).pack(side="right", padx=8)

        steps = tk.Frame(st, bg=PANEL)
        steps.pack(fill="x", padx=18, pady=(0, 16))
        self.chips = []
        for i, (_, text) in enumerate(STEPS):
            lb = tk.Label(steps, text=text, font=F_SM, bg=PANEL2, fg=MUTED, padx=8, pady=5,
                          highlightthickness=1, highlightbackground=LINE)
            lb.pack(side="left", padx=(0, 4))
            self.chips.append(lb)
            if i < len(STEPS) - 1:
                tk.Label(steps, text="›", bg=PANEL, fg=MUTED).pack(side="left", padx=(0, 4))

        ch = card(p)
        ch.pack(fill="x", padx=28)
        tk.Label(ch, text="CANAL AL VOLVER", font=("Consolas", 8, "bold"), bg=PANEL, fg=MUTED).pack(anchor="w", padx=18, pady=(14, 6))
        row = tk.Frame(ch, bg=PANEL)
        row.pack(fill="x", padx=18, pady=(0, 6))
        fixed = int(self.cfg.get("channel_fixed", 0))
        self.chan_seg = Segmented(row, [("0", "Último")] + [(str(i), str(i)) for i in range(1, 6)],
                                  str(fixed), self.on_channel)
        self.chan_seg.pack(side="left")
        self.chan_lbl = tk.Label(row, text="", font=F_SM, bg=PANEL, fg=MUTED)
        self.chan_lbl.pack(side="left", padx=14)
        tk.Label(ch, text="«Último» recuerda el canal en el que estabas (se lee del título de la ventana del juego).",
                 font=F_SM, bg=PANEL, fg=MUTED, wraplength=520, justify="left").pack(anchor="w", padx=18, pady=(0, 14))
        return p

    def build_settings(self, parent):
        p = tk.Frame(parent, bg=BG)
        p.grid(row=0, column=0, sticky="nsew")
        tk.Label(p, text="Ajustes", font=F_H1, bg=BG, fg=FG).pack(anchor="w", padx=28, pady=(24, 14))
        c = card(p)
        c.pack(fill="x", padx=28)
        self.vars = {}

        def row(label, hint, widget_factory):
            r = tk.Frame(c, bg=PANEL)
            r.pack(fill="x", padx=18, pady=10)
            widget_factory(r).pack(side="right", padx=(12, 0))
            left = tk.Frame(r, bg=PANEL)
            left.pack(side="left")
            tk.Label(left, text=label, font=F_UI, bg=PANEL, fg=FG).pack(anchor="w")
            tk.Label(left, text=hint, font=F_SM, bg=PANEL, fg=MUTED, justify="left", wraplength=300).pack(anchor="w")
            tk.Frame(c, bg=LINE, height=1).pack(fill="x")

        def spin(key, lo, hi, step=1):
            def make(parent):
                v = tk.StringVar(value=str(self.cfg[key]))
                self.vars[key] = v
                return tk.Spinbox(parent, from_=lo, to=hi, increment=step, textvariable=v, width=6, font=F_UI,
                                  bg=PANEL2, fg=FG, buttonbackground=PANEL2, insertbackground=FG, relief="flat",
                                  highlightthickness=1, highlightbackground=LINE)
            return make

        def check(key):
            def make(parent):
                v = tk.BooleanVar(value=bool(self.cfg[key]))
                self.vars[key] = v
                return tk.Checkbutton(parent, variable=v, bg=PANEL, activebackground=PANEL, selectcolor=PANEL2,
                                      fg=ACCENT, activeforeground=ACCENT)
            return make

        self.input_seg = None

        def mk_input(parent):
            self.input_seg = Segmented(parent, [("background", "segundo plano"), ("foreground", "mouse real")],
                                       self.cfg["input"], lambda k: None)
            return self.input_seg

        row("Tipo de click", "«segundo plano» (recomendado): con el juego detrás de otras ventanas, sin tocar tu mouse ni tu teclado. "
            "No lo minimices. «mouse real» es el método viejo.", mk_input)
        row("Revisar cada (segundos)", "Cada cuánto mira la ventana del juego.", spin("poll_seconds", 1, 30))
        row("Intentos antes de pausar", "Si una pantalla no avanza, frena 60 s para no insistir.", spin("max_attempts", 2, 10))
        row("Si un click falla, usar el mouse real", "Si un paso se repite sin avanzar, ese click usa el mouse real (el juego pasa al frente un instante).",
            check("fallback_real_mouse"))
        row("Abrir el juego si está minimizado", "Si se corta el internet y el juego está minimizado, lo restaura sin darle "
            "el foco, reloguea y lo vuelve a minimizar.", check("restore_minimized"))
        row("Al cortarse internet, juego al frente", "Tildado: abre el juego y lo deja AL FRENTE (no lo vuelve a minimizar). "
            "Destildado: lo deja de fondo, detrás de las demás ventanas.", check("outage_front"))
        row("Restaurarlo en un corte largo (s)", "Si el internet lleva tantos segundos caído y el juego está minimizado, "
            "lo restaura para ver el cartel y reloguear apenas vuelva (0 = no).", spin("peek_during_outage_s", 0, 600))
        row("Dejarlo mirando tras un corte (s)", "Cuánto se queda restaurado después de un corte antes de volver a minimizarlo.",
            spin("peek_dwell_s", 5, 300))
        row("Revisarlo cada (minutos)", "Si sigue minimizado, lo restaura brevemente cada tanto para ver si se cayó "
            "(0 = nunca).", spin("peek_every_min", 0, 120))
        row("Al cerrar, seguir en la bandeja", "La X oculta la ventana y el programa sigue funcionando en los iconos ocultos "
            "(clic derecho en el icono → Salir).", check("minimize_to_tray"))
        row("Avisos desde la bandeja", "Notificación cuando vuelve el internet o termina de reloguear (solo con la ventana oculta).",
            check("tray_notifications"))
        row("Iniciar oculto en la bandeja", "Al abrir el programa no muestra la ventana.", check("start_in_tray"))
        row("Medir la experiencia", "Lee el % de exp del HUD cada ~10 s (con el juego visible) para calcular el ritmo por hora.",
            check("exp_tracking"))
        row("Avisar si no gana exp (minutos)", "Aviso si pasan estos minutos sin ganar experiencia: Angel Baby parado o personaje "
            "muerto (0 = nunca).", spin("exp_stall_min", 0, 240))
        row("Activar el Angel Baby al volver", "Tras reloguear: Ctrl+R y tilda Attack (donde esté parado el personaje).",
            check("angel_baby_after_relog"))
        row("Cerrar el panel del Angel Baby", "Lo cierra (con su botón ✕) cuando termina de activarlo.", check("ab_close_panel"))
        row("Cerrar aviso de Trade PIN", "Aprieta ✘ en «¿Quieres poner un Trade PIN?».", check("dismiss_trade_pin"))
        bar = tk.Frame(p, bg=BG)
        bar.pack(fill="x", padx=28, pady=16)
        Btn(bar, "Guardar", self.save_settings, primary=True).pack(side="left")
        self.saved_lbl = tk.Label(bar, text="", font=F_SM, bg=BG, fg=ACCENT)
        self.saved_lbl.pack(side="left", padx=12)
        return p

    def build_net(self, parent):
        p = tk.Frame(parent, bg=BG)
        p.grid(row=0, column=0, sticky="nsew")
        head = tk.Frame(p, bg=BG)
        head.pack(fill="x", padx=28, pady=(24, 4))
        tk.Label(head, text="Conexión", font=F_H1, bg=BG, fg=FG).pack(side="left")
        self.net_seg = Segmented(head, netmon.PERIODS, "hoy", lambda k: self.paint_net())
        self.net_seg.pack(side="right", pady=(10, 0))
        tk.Label(p, text="Registro de los cortes de internet medidos desde esta PC, con hora de inicio y fin. "
                 "Sirve como evidencia para el reclamo al proveedor.", font=F_UI, bg=BG, fg=MUTED,
                 justify="left", wraplength=640).pack(anchor="w", padx=28)

        cur = card(p)
        cur.pack(fill="x", padx=28, pady=(12, 8))
        row = tk.Frame(cur, bg=PANEL)
        row.pack(fill="x", padx=18, pady=10)
        self.net_dot = tk.Label(row, text="●", font=("Segoe UI", 16), bg=PANEL, fg=MUTED)
        self.net_dot.pack(side="left")
        col = tk.Frame(row, bg=PANEL)
        col.pack(side="left", padx=10)
        self.net_state = tk.Label(col, text="Midiendo…", font=("Segoe UI", 13, "bold"), bg=PANEL, fg=FG, anchor="w")
        self.net_state.pack(anchor="w")
        self.net_sub = tk.Label(col, text="", font=F_SM, bg=PANEL, fg=MUTED, anchor="w")
        self.net_sub.pack(anchor="w")

        grid = tk.Frame(p, bg=BG)
        grid.pack(fill="x", padx=28)
        self.net_cards = {}
        defs = [("count", "Cortes"), ("down", "Tiempo sin internet"), ("longest", "Corte más largo"),
                ("avg", "Duración promedio"), ("avail", "Disponibilidad"), ("loss", "Pruebas fallidas"),
                ("lat", "Latencia prom. / máx."), ("game", "Caídas del juego")]
        for i, (k, title) in enumerate(defs):
            c = tk.Frame(grid, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
            c.grid(row=i // 4, column=i % 4, sticky="nsew", padx=(0 if i % 4 == 0 else 6, 0), pady=(0, 6))
            grid.columnconfigure(i % 4, weight=1, uniform="nc")
            tk.Label(c, text=title, font=F_SM, bg=PANEL, fg=MUTED, anchor="w").pack(anchor="w", padx=12, pady=(8, 0))
            v = tk.Label(c, text="—", font=("Segoe UI", 14, "bold"), bg=PANEL, fg=FG, anchor="w")
            v.pack(anchor="w", padx=12)
            s = tk.Label(c, text="", font=F_SM, bg=PANEL, fg=MUTED, anchor="w")
            s.pack(anchor="w", padx=12, pady=(0, 8))
            self.net_cards[k] = (v, s)

        tl = card(p)
        tl.pack(fill="x", padx=28, pady=(2, 8))
        th = tk.Frame(tl, bg=PANEL)
        th.pack(fill="x", padx=18, pady=(8, 2))
        tk.Label(th, text="ÚLTIMAS 24 HORAS", font=("Consolas", 8, "bold"), bg=PANEL, fg=MUTED).pack(side="left")
        tk.Label(th, text="■ cortes", font=F_SM, bg=PANEL, fg=DANGER).pack(side="right")
        tk.Label(th, text="■ medido   ", font=F_SM, bg=PANEL, fg="#2c7a76").pack(side="right")
        tk.Label(th, text="■ sin medir   ", font=F_SM, bg=PANEL, fg=LINE).pack(side="right")
        self.net_canvas = tk.Canvas(tl, height=46, bg=PANEL, highlightthickness=0)
        self.net_canvas.pack(fill="x", padx=18, pady=(0, 8))

        tb = card(p)
        tb.pack(fill="both", expand=True, padx=28, pady=(0, 8))
        cols = (("ini", "Inicio", 150), ("fin", "Fin", 150), ("dur", "Duración", 110), ("causa", "Causa orientativa", 210))
        self.net_tree = ttk.Treeview(tb, columns=[c[0] for c in cols], show="headings", style="Net.Treeview", height=5)
        for k, t, w in cols:
            self.net_tree.heading(k, text=t, anchor="w")
            self.net_tree.column(k, width=w, anchor="w", stretch=(k == "causa"))
        self.net_tree.tag_configure("live", foreground=DANGER)
        self.net_tree.tag_configure("local", foreground=WARN)
        tsb = ttk.Scrollbar(tb, command=self.net_tree.yview, style="Dark.Vertical.TScrollbar")
        self.net_tree.config(yscrollcommand=tsb.set)
        tsb.pack(side="right", fill="y")
        self.net_tree.pack(side="left", fill="both", expand=True)

        bar = tk.Frame(p, bg=BG)
        bar.pack(fill="x", padx=28, pady=(0, 14))
        Btn(bar, "Exportar informe", self.export_net, primary=True).pack(side="left")
        Btn(bar, "Copiar resumen", self.copy_net).pack(side="left", padx=8)
        Btn(bar, "Abrir carpeta de datos", lambda: self.open_path(netmon.DATA)).pack(side="left")
        self.net_msg = tk.Label(bar, text="", font=F_SM, bg=BG, fg=ACCENT)
        self.net_msg.pack(side="left", padx=12)
        return p

    def _outage_secs(self):
        """Segundos que lleva el corte de internet actual (0 si no hay)."""
        try:
            o = self.net.snapshot()["outage"]
            return time.time() - o["inicio"] if o else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def on_outage_end(self, rec):
        """Volvio internet tras un corte: si el juego esta minimizado, pedir que se lo restaure y revise."""
        if not self.running:
            core.log.info("Internet volvió tras %s, pero la vigilancia está detenida: no reviso el juego.",
                          netmon.fmt_dur(rec["dur"]))
            return
        if rec["dur"] >= self.cfg.get("peek_min_outage", 3):
            self.bot.peek_reason = f"internet volvió tras {netmon.fmt_dur(rec['dur'])}"
            self.bot.want_peek = True
        if rec["dur"] >= 30:   # el cartel de desconexion puede aparecer un rato despues: revisar de nuevo mas tarde
            now = time.time()
            self.bot.followups = [now + 60, now + 180]

    def build_exp(self, parent):
        p = tk.Frame(parent, bg=BG)
        p.grid(row=0, column=0, sticky="nsew")
        head = tk.Frame(p, bg=BG)
        head.pack(fill="x", padx=28, pady=(24, 4))
        tk.Label(head, text="Experiencia", font=F_H1, bg=BG, fg=FG).pack(side="left")
        self.exp_seg = Segmented(head, [("ses", "Sesión"), ("1h", "1 hora"), ("hoy", "Hoy"), ("todo", "Todo")], "ses",
                                 lambda k: self.paint_exp())
        self.exp_seg.pack(side="right", pady=(10, 0))
        tk.Label(p, text="Lee el % de experiencia del HUD y calcula el ritmo por hora. Solo mide mientras la vigilancia está "
                 "activa y el juego está visible (no minimizado).", font=F_UI, bg=BG, fg=MUTED, justify="left",
                 wraplength=640).pack(anchor="w", padx=28)

        cur = card(p)
        cur.pack(fill="x", padx=28, pady=(12, 8))
        top = tk.Frame(cur, bg=PANEL)
        top.pack(fill="x", padx=18, pady=(12, 4))
        self.exp_pct = tk.Label(top, text="— %", font=("Georgia", 22), bg=PANEL, fg=FG)
        self.exp_pct.pack(side="left")
        col = tk.Frame(top, bg=PANEL)
        col.pack(side="left", padx=14)
        self.exp_state = tk.Label(col, text="", font=("Segoe UI", 11, "bold"), bg=PANEL, fg=MUTED, anchor="w")
        self.exp_state.pack(anchor="w")
        self.exp_sub = tk.Label(col, text="", font=F_SM, bg=PANEL, fg=MUTED, anchor="w")
        self.exp_sub.pack(anchor="w")
        self.exp_bar = tk.Canvas(cur, height=12, bg=PANEL, highlightthickness=0)
        self.exp_bar.pack(fill="x", padx=18, pady=(2, 12))

        grid = tk.Frame(p, bg=BG)
        grid.pack(fill="x", padx=28)
        self.exp_cards = {}
        defs = [("rate", "Exp por hora"), ("recent", "Ritmo (últ. 30 min)"), ("gained", "Ganada"), ("active", "Tiempo activo"),
                ("eta", "Próximo nivel en"), ("levels", "Niveles subidos"), ("lost", "Exp perdida"), ("stall", "Sin ganar hace")]
        for i, (k, title) in enumerate(defs):
            c = tk.Frame(grid, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
            c.grid(row=i // 4, column=i % 4, sticky="nsew", padx=(0 if i % 4 == 0 else 6, 0), pady=(0, 6))
            grid.columnconfigure(i % 4, weight=1, uniform="ec")
            tk.Label(c, text=title, font=F_SM, bg=PANEL, fg=MUTED, anchor="w").pack(anchor="w", padx=12, pady=(8, 0))
            v = tk.Label(c, text="—", font=("Segoe UI", 14, "bold"), bg=PANEL, fg=FG, anchor="w")
            v.pack(anchor="w", padx=12)
            s = tk.Label(c, text="", font=F_SM, bg=PANEL, fg=MUTED, anchor="w")
            s.pack(anchor="w", padx=12, pady=(0, 8))
            self.exp_cards[k] = (v, s)

        ch = card(p)
        ch.pack(fill="both", expand=True, padx=28, pady=(2, 8))
        tk.Label(ch, text="EXPERIENCIA GANADA · ÚLTIMAS 3 HORAS (barras de 10 min)", font=("Consolas", 8, "bold"),
                 bg=PANEL, fg=MUTED).pack(anchor="w", padx=18, pady=(10, 2))
        self.exp_canvas = tk.Canvas(ch, height=110, bg=PANEL, highlightthickness=0)
        self.exp_canvas.pack(fill="both", expand=True, padx=18, pady=(0, 10))

        bar = tk.Frame(p, bg=BG)
        bar.pack(fill="x", padx=28, pady=(0, 14))
        Btn(bar, "Reiniciar sesión", self.exp_reset).pack(side="left")
        Btn(bar, "Abrir carpeta de datos", lambda: self.open_path(netmon.DATA)).pack(side="left", padx=8)
        return p

    def exp_reset(self):
        self.bot.exp.reset_session()
        core.log.info("Sesión de experiencia reiniciada.")
        self.paint_exp()

    def refresh_exp(self):
        if self.current_page == "exp":
            try:
                self.paint_exp()
            except Exception:  # noqa: BLE001
                core.log.exception("error al dibujar la pestaña Experiencia")
        self.after(2000, self.refresh_exp)

    def paint_exp(self):
        now = time.time()
        ex = self.bot.exp
        k = self.exp_seg.value
        if k == "ses":
            st = ex.stats(None, now)
        elif k == "1h":
            st = ex.stats(now - 3600, now)
        elif k == "hoy":
            lt = time.localtime(now)
            st = ex.stats(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)), now)
        else:
            st = ex.stats(0, now)
        recent = ex.stats(now - 1800, now)

        info = self.bot.info.get("exp")
        pct = ex.last_pct
        self.exp_pct.config(text="— %" if pct is None else f"{pct:.2f} %")
        if info and now - info[1] < 60:
            self.exp_state.config(text="Leyendo del HUD", fg=ACCENT)
            self.exp_sub.config(text=f"última lectura hace {int(now - info[1])} s")
        else:
            why = ("la vigilancia está detenida" if not self.running else
                   "el juego está minimizado" if self.bot.info.get("phase") == "minimized" else
                   "esperando una lectura (¿ventana en 1366×768, en el mundo y sin carteles?)")
            self.exp_state.config(text="Sin lectura", fg=WARN)
            self.exp_sub.config(text=why)
        cv = self.exp_bar
        cv.delete("all")
        W = max(cv.winfo_width(), 200)
        cv.create_rectangle(0, 2, W, 10, fill=BG, outline=LINE)
        if pct is not None:
            cv.create_rectangle(0, 2, W * pct / 100.0, 10, fill=ACCENT, outline="")

        def put(key, value, sub="", color=FG):
            v, s = self.exp_cards[key]
            v.config(text=value, fg=color)
            s.config(text=sub)

        eta = ex.eta_next_level(now)
        stall = ex.stalled_for(now)
        limit = (self.cfg.get("exp_stall_min", 10) or 0) * 60
        put("rate", "—" if st["rate"] is None else f"{st['rate']:.1f} %/h",
            "" if st["rate"] is not None else "hacen falta ~2 min de datos")
        put("recent", "—" if recent["rate"] is None else f"{recent['rate']:.1f} %/h")
        put("gained", f"{st['gained']:.2f} %", f"≈ {st['gained'] / 100:.2f} niveles")
        put("active", expmon.fmt_dur(st["active"]), "con lectura continua")
        put("eta", expmon.fmt_dur(eta), "" if pct is None else f"faltan {100 - pct:.2f} %")
        put("levels", str(st["levels"]))
        put("lost", f"{st['lost']:.2f} %", "por muertes" if st["lost"] > 0 else "")
        put("stall", "—" if stall is None else expmon.fmt_dur(stall), "",
            WARN if (stall is not None and limit and stall >= limit) else FG)

        c = self.exp_canvas
        c.delete("all")
        W, H = max(c.winfo_width(), 300), max(c.winfo_height(), 80)
        data = ex.buckets(18, 600, now)
        top = max(max(data), 0.5)
        bw = W / len(data)
        base = H - 20
        c.create_line(0, base, W, base, fill=LINE)
        for i, v in enumerate(data):
            h = (base - 22) * v / top
            x0 = i * bw + 3
            if v > 0:
                c.create_rectangle(x0, base - h, x0 + bw - 6, base, fill=ACCENT, outline="")
                c.create_text(x0 + (bw - 6) / 2, base - h - 7, text=f"{v:.1f}", fill=MUTED, font=F_MONO)
            if i % 3 == 0:
                t = now - (len(data) - i) * 600
                c.create_text(x0, H - 8, text=time.strftime("%H:%M", time.localtime(t)), fill=MUTED, font=F_MONO, anchor="w")

    def open_path(self, path):
        try:
            path.mkdir(exist_ok=True)
            os.startfile(str(path))
        except OSError:
            pass

    def net_flash(self, text):
        self.net_msg.config(text=text)
        self.after(5000, lambda: self.net_msg.config(text=""))

    def export_net(self):
        st = netmon.stats(self.net_seg.value, self.net)
        path = netmon.export_report(st, self.cfg.get("isp", ""))
        self.net_flash(f"Guardado en informes\\{path.name}")
        core.log.info("Informe de conexión exportado: %s", path.name)
        try:
            os.startfile(str(path))
        except OSError:
            pass

    def copy_net(self):
        st = netmon.stats(self.net_seg.value, self.net)
        self.clipboard_clear()
        self.clipboard_append(netmon.summary_text(st, self.cfg.get("isp", "")))
        self.net_flash("Resumen copiado ✓")

    def build_right(self, body):
        r = tk.Frame(body, bg=PANEL, width=330, highlightthickness=1, highlightbackground=LINE)
        r.grid(row=0, column=2, sticky="ns")
        r.pack_propagate(False)
        head = tk.Frame(r, bg=PANEL)
        head.pack(fill="x", padx=16, pady=(16, 8))
        tk.Label(head, text="Clientes", font=("Segoe UI", 10, "bold"), bg=PANEL, fg=FG).pack(side="left")
        self.client_count = tk.Label(head, text="0 corriendo", font=F_MONO, bg=PANEL, fg=MUTED)
        self.client_count.pack(side="left", padx=10)

        cc = tk.Frame(r, bg=PANEL2, highlightthickness=1, highlightbackground=LINE)
        cc.pack(fill="x", padx=16)
        self.client_title = tk.Label(cc, text="—", font=("Segoe UI", 10, "bold"), bg=PANEL2, fg=FG,
                                     wraplength=270, justify="left", anchor="w")
        self.client_title.pack(anchor="w", padx=12, pady=(10, 2))
        self.client_sub = tk.Label(cc, text="", font=F_MONO, bg=PANEL2, fg=MUTED, anchor="w")
        self.client_sub.pack(anchor="w", padx=12)
        self.client_state = tk.Label(cc, text="", font=F_SM, bg=PANEL2, fg=MUTED, anchor="w")
        self.client_state.pack(anchor="w", padx=12, pady=(2, 10))

        head2 = tk.Frame(r, bg=PANEL)
        head2.pack(fill="x", padx=16, pady=(18, 6))
        tk.Label(head2, text="Actividad", font=("Segoe UI", 10, "bold"), bg=PANEL, fg=FG).pack(side="left")
        self.event_count = tk.Label(head2, text="0 eventos", font=F_MONO, bg=PANEL, fg=MUTED)
        self.event_count.pack(side="right")
        wrap = tk.Frame(r, bg=PANEL)
        wrap.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.log = tk.Text(wrap, bg=PANEL, fg=FG, font=F_SM, bd=0, highlightthickness=0, wrap="word",
                           cursor="arrow", spacing1=2, spacing3=8, state="disabled")
        sb = ttk.Scrollbar(wrap, command=self.log.yview, style="Dark.Vertical.TScrollbar")
        self.log.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_config("t", font=F_MONO, foreground=MUTED)
        self.log.tag_config("warn", foreground=WARN)
        self.log.tag_config("err", foreground=DANGER)

    # ------------------------------------------------------------ acciones
    def show(self, key):
        self.current_page = key
        {"main": self.page_main, "net": self.page_net, "exp": self.page_exp, "cfg": self.page_cfg}[key].tkraise()
        if key == "net":
            self._net_sig = None
            self.after(50, self.paint_net)
        elif key == "exp":
            self.after(50, self.paint_exp)
        for k, lb in self.nav.items():
            lb.config(bg=PANEL2 if k == key else PANEL, fg=ACCENT if k == key else FG)

    def on_mode(self, key):
        if key == "live":
            if not messagebox.askyesno(
                    "Activar modo live",
                    "En modo live el programa va a hacer clicks reales en el juego cuando detecte una desconexión.\n\n"
                    "¿Activarlo?", parent=self):
                self.mode.select("ensayo", False)
                return
            self.cfg["dry_run"] = False
            core.log.info("Modo LIVE activado: ahora hace clicks de verdad.")
        else:
            self.cfg["dry_run"] = True
            core.log.info("Modo ensayo: solo anota lo que haría.")
        self.persist()
        self.update_sub()

    def on_channel(self, key):
        self.cfg["channel_fixed"] = int(key)
        self.persist()
        core.log.info("Canal al volver: %s", "último recordado" if key == "0" else f"fijo {key}")

    def update_sub(self):
        self.sub_lbl.config(text="Modo ensayo: solo anota lo que haría" if self.cfg["dry_run"]
                            else "Modo live: hace clicks reales en el juego")

    def persist(self):
        core.save_cfg(self.cfg)

    def save_settings(self):
        try:
            for k, v in self.vars.items():
                val = v.get()
                self.cfg[k] = val if isinstance(val, bool) else int(val)
            self.cfg["input"] = self.input_seg.value
        except ValueError:
            messagebox.showerror("Ajustes", "Los números no son válidos.", parent=self)
            return
        self.persist()
        self.saved_lbl.config(text="Guardado ✓")
        self.after(2500, lambda: self.saved_lbl.config(text=""))

    def toggle_run(self):
        if self.running:
            self.stop_evt.set()
            self.running = False
            self.bot.info["phase"] = "idle"
            self.start_btn.set("Iniciar", primary=True)
            core.log.info("Vigilancia detenida.")
        else:
            self.stop_evt = threading.Event()
            self.bot.reset()
            self.running = True
            self.start_btn.set("Detener", primary=False)
            core.log.info("Vigilancia iniciada (%s, canal %s).", "ENSAYO" if self.cfg["dry_run"] else "LIVE",
                          self.bot.channel())
            threading.Thread(target=self.worker, args=(self.stop_evt,), daemon=True).start()

    def worker(self, evt):
        while not evt.is_set():
            try:
                self.bot.step()
            except Exception:  # noqa: BLE001
                core.log.exception("error en el ciclo")
            evt.wait(self.bot.next_delay)

    def test_ab(self):
        threading.Thread(target=lambda: self.bot.run_ab_now(), daemon=True).start()

    def test_once(self):
        def run():
            try:
                self.bot.step()
            except Exception:  # noqa: BLE001
                core.log.exception("error en la prueba")
        core.log.info("Prueba manual (un análisis).")
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------ refresco
    def handle_tray(self):
        try:
            while True:
                ev = self.tray_q.get_nowait()
                if ev == "open":
                    self.show_window()
                elif ev == "toggle":
                    self.toggle_run()
                elif ev == "exit":
                    self.quit_app()
                    return True
        except queue.Empty:
            pass
        return False

    def tick(self):
        try:
            if self.handle_tray():
                return
            self._tick_body()
        except Exception:  # noqa: BLE001
            core.log.exception("error al actualizar la interfaz")
        self.after(250, self.tick)

    def _tick_body(self):
        try:
            while True:
                t, msg, lvl = self.q.get_nowait()
                if (msg.startswith(("de vuelta en el mundo", "Angel Baby activado", "Internet: volvió", "Sin ganar experiencia",
                                    "¡Subiste de nivel", "Bajó la experiencia", "Entré a", "El servidor no responde")) or "no avanza" in msg):
                    self.notify("AutoRelog", msg)
                self.log.config(state="normal")
                tag = "err" if lvl >= logging.ERROR else "warn" if lvl >= logging.WARNING else ""
                self.log.insert("1.0", msg + "\n", tag)
                self.log.insert("1.0", t + "  ", "t")
                self.log.config(state="disabled")
                self.n_events += 1
                self.event_count.config(text=f"{self.n_events} eventos")
        except queue.Empty:
            pass
        if int(self.log.index("end-1c").split(".")[0]) > 500:  # recortar el panel (lo viejo queda en autorelog.log)
            self.log.config(state="normal")
            self.log.delete("400.0", "end")
            self.log.config(state="disabled")
        self.paint_state()

    def paint_state(self):
        info = self.bot.info
        phase = info["phase"] if self.running else "idle"
        if self.running and phase == "idle":
            phase = "unknown"
        self.state_lbl.config(text=("Vigilando · " if self.running else "") + PHASE_TEXT.get(phase, phase)
                              if self.running else "Detenido")
        color = MUTED if not self.running else (ACCENT if phase == "world" else WARN if phase in
                                                ("nowindow", "unknown", "offline") else DANGER)
        self.dot.config(fg=color)
        cur = PHASE_TO_STEP.get(phase, -1) if self.running else -1
        for i, lb in enumerate(self.chips):
            if i == cur:
                lb.config(bg=ACCENT, fg=BG, highlightbackground=ACCENT)
            elif 0 <= cur and i < cur:
                lb.config(bg=PANEL2, fg=ACCENT, highlightbackground=LINE)
            else:
                lb.config(bg=PANEL2, fg=MUTED, highlightbackground=LINE)
        last = self.bot.state.get("channel")
        self.chan_lbl.config(text=f"Recordado: {self.bot.state.get('server', 'Athena')}-{last}" if last
                             else "Aún no recordó ningún canal")

    def refresh_net_header(self):
        try:
            s = self.net.snapshot()
            vig = "Vigilando el juego" if self.running else "Vigilancia detenida"
            if s["outage"]:
                dur = netmon.fmt_dur(time.time() - s["outage"]["inicio"])
                self.net_lbl.config(text=f"● SIN INTERNET · {dur}", fg=DANGER)
                self.tray.update(f"AutoRelog · {vig}\nSIN INTERNET · {dur}", down=True)
            elif s["online"]:
                ms = s["ms"] or 0
                self.net_lbl.config(text=f"● Internet · {ms:.0f} ms", fg=WARN if ms > 150 else ACCENT)
                self.tray.update(f"AutoRelog · {vig}\nInternet OK · {ms:.0f} ms")
            else:
                self.net_lbl.config(text="● Internet · midiendo…", fg=MUTED)
        except Exception:  # noqa: BLE001
            pass
        self.after(1000, self.refresh_net_header)

    def refresh_net(self):
        if self.current_page == "net":
            try:
                self.paint_net()
            except Exception:  # noqa: BLE001
                core.log.exception("error al dibujar la pestaña Conexión")
        self.after(2000, self.refresh_net)

    def paint_net(self):
        now = time.time()
        key = self.net_seg.value
        st = netmon.stats(key, self.net, now)
        snap = self.net.snapshot()
        o = snap["outage"]
        if o:
            self.net_dot.config(fg=DANGER)
            self.net_state.config(text=f"Sin internet desde las {netmon.fmt_ts(o['inicio']).split(' ')[1]}")
            self.net_sub.config(text=f"Lleva {netmon.fmt_dur(now - o['inicio'])} · "
                                f"{netmon.CAUSAS[netmon.cause_of(o)]}")
        elif snap["online"]:
            self.net_dot.config(fg=ACCENT)
            self.net_state.config(text="En línea")
            gw = f" · router {snap['gateway']}" if snap["gateway"] else ""
            self.net_sub.config(text=f"{(snap['ms'] or 0):.0f} ms{gw}")
        else:
            self.net_dot.config(fg=MUTED)
            self.net_state.config(text="Midiendo…")
            self.net_sub.config(text="")

        def put(k, value, sub=""):
            v, s = self.net_cards[k]
            v.config(text=value)
            s.config(text=sub)

        longest = max(st["cuts"], key=lambda c: c["dur"]) if st["cuts"] else None
        put("count", str(st["count"]), f"{st['n_prov']} proveedor · {st['n_local']} red local")
        put("down", netmon.fmt_dur(st["down"]) if st["count"] else "0 s", f"medido {st['coverage']:.0f}% del período")
        put("longest", netmon.fmt_dur(st["longest"]), netmon.fmt_ts(longest["inicio"], True) if longest else "")
        put("avg", netmon.fmt_dur(st["avg"]), "")
        put("avail", "—" if st["availability"] is None else f"{st['availability']:.2f}%",
            f"{netmon.fmt_dur(st['monitored'])} medidos")
        put("loss", "—" if st["loss"] is None else f"{st['loss']:.2f}%", f"{st['failed']} de {st['probes']} pruebas")
        put("lat", "—" if st["lat_avg"] is None else f"{st['lat_avg']:.0f} / {st['lat_max']:.0f} ms", "")
        put("game", str(st["game_drops"]), f"reingreso prom. {netmon.fmt_dur(st['reentry_avg'])}" if st["reentry_avg"] else "")

        self.draw_timeline(now)

        sig = (key, len(st["cuts"]), bool(o), int(now // 2) if o else 0)
        if sig != self._net_sig:
            self._net_sig = sig
            self.net_tree.delete(*self.net_tree.get_children())
            for c in st["cuts"][:300]:
                live = c.get("en_curso")
                tags = ("live",) if live else (("local",) if c["causa"] == "red_local" else ())
                self.net_tree.insert("", "end", tags=tags, values=(
                    netmon.fmt_ts(c["inicio"]), "en curso" if live else netmon.fmt_ts(c["fin"]),
                    netmon.fmt_dur(c["dur"]), netmon.CAUSAS[c["causa"]]))

    def draw_timeline(self, now):
        cv = self.net_canvas
        cv.delete("all")
        W = max(cv.winfo_width(), 300)
        p0 = now - 86400
        x = lambda t: max(0, min(W, (t - p0) / 86400 * W))  # noqa: E731
        cv.create_rectangle(0, 4, W, 28, fill=BG, outline=LINE)
        for a, b in netmon.sessions_list(now, self.net):
            if b > p0:
                cv.create_rectangle(x(a), 5, max(x(b), x(a) + 1), 27, fill="#1d4b49", outline="")
        for c in netmon.cuts_list(now, self.net):
            if c["fin"] > p0:
                x0 = x(c["inicio"])
                cv.create_rectangle(x0, 5, max(x(c["fin"]), x0 + 3), 27, fill=DANGER, outline="")
        t = (int(p0 // 3600) + 1) * 3600
        while t < now:
            if time.localtime(t).tm_hour % 4 == 0:
                cv.create_line(x(t), 28, x(t), 32, fill=MUTED)
                cv.create_text(x(t), 39, text=time.strftime("%H:%M", time.localtime(t)), fill=MUTED, font=F_MONO)
            t += 3600

    def refresh_client(self):
        try:
            hwnd = core.find_window(self.cfg)
            if hwnd:
                title = core.win32gui.GetWindowText(hwnd)
                _, pid = core.win32process.GetWindowThreadProcessId(hwnd)
                m = core.TITLE_RE.search(title)
                self.client_count.config(text="1 corriendo", fg=ACCENT)
                self.client_title.config(text=title)
                self.client_sub.config(text=f"PID {pid}")
                self.client_state.config(text="En el mundo" if m else "En login / selección de personaje",
                                         fg=ACCENT if m else WARN)
                if m and not self.running:  # recordar canal aun sin vigilar
                    ch = int(m.group("ch"))
                    if self.bot.state.get("channel") != ch:
                        self.bot.state.update(channel=ch, server=m.group("server"))
                        core.save_state(self.bot.state)
            else:
                self.client_count.config(text="0 corriendo", fg=MUTED)
                self.client_title.config(text="No se encontró el juego")
                self.client_sub.config(text="")
                self.client_state.config(text="Abrí Angels Online para que lo vea", fg=MUTED)
        except Exception:  # noqa: BLE001
            pass
        self.after(2000, self.refresh_client)

    def hidden(self):
        return self.state() == "withdrawn"

    def show_window(self):
        self.deiconify()
        self.state("normal")
        self.lift()
        self.attributes("-topmost", True)
        self.after(250, lambda: self.attributes("-topmost", False))
        self.focus_force()

    def notify(self, title, text):
        """Aviso desde la bandeja, solo si la ventana esta oculta."""
        if self.hidden() and self.cfg.get("tray_notifications", True):
            self.tray.balloon(title, text)

    def on_close(self):
        """La X oculta la ventana: el programa sigue corriendo en la bandeja (iconos ocultos)."""
        if not self.cfg.get("minimize_to_tray", True) or not (self.tray.is_alive() and self.tray.ready.is_set()):
            self.quit_app()   # sin icono en la bandeja no hay forma de recuperar la ventana: cerrar de verdad
            return
        self.withdraw()
        if not self._hint_shown:
            self._hint_shown = True
            self.tray.balloon("AutoRelog sigue funcionando",
                              "Está en los iconos ocultos. Clic derecho en el icono → Salir para cerrarlo del todo.")

    def quit_app(self):
        self.stop_evt.set()
        self.net.stop()
        self.net.join(3)  # deja cerrada la sesion de medicion en los archivos
        self.tray.stop()
        self.destroy()


def single_instance():
    """Si ya hay una instancia (quiza oculta en la bandeja), la muestra y sale: dos medirian a la vez en los mismos archivos."""
    import win32event
    import winerror
    handle = win32event.CreateMutex(None, False, "AutoRelog_SingleInstance")
    if core.win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        hw = core.win32gui.FindWindow(tray.CLASS, None)
        if hw:
            core.win32gui.PostMessage(hw, tray.WM_SHOW, 0, 0)
        else:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, "AutoRelog ya está abierto, pero no encuentro su icono para mostrarlo.\n"
                                             "Cerralo desde el Administrador de tareas (pythonw.exe) y volvé a abrirlo.",
                                             "AutoRelog", 0x40)
        sys.exit(0)
    return handle


if __name__ == "__main__":
    _mutex = single_instance()
    App().mainloop()
