"""Icono en la bandeja del sistema (iconos ocultos) usando solo pywin32."""
import struct
import threading
from pathlib import Path

import cv2
import numpy as np
import win32api
import win32con
import win32gui

ROOT = Path(__file__).parent
CLASS = "AutoRelogTray"
WM_TRAY = win32con.WM_USER + 20
WM_SHOW = win32con.WM_USER + 21      # otra instancia pide que se muestre la ventana
ID_OPEN, ID_TOGGLE, ID_EXIT = 1001, 1002, 1003
COLORS = {"ok": (191, 202, 111), "down": (95, 103, 229)}   # BGR: verde agua / rojo


def _make_ico(path, bgr):
    entries = []
    for sz in (16, 24, 32, 48, 64):
        img = np.zeros((sz, sz, 4), np.uint8)
        cv2.circle(img, (sz // 2, sz // 2), sz // 2 - 1, (*bgr, 255), -1, cv2.LINE_AA)
        scale = sz / 34.0
        (tw, th), base = cv2.getTextSize("A", cv2.FONT_HERSHEY_DUPLEX, scale, max(1, round(scale * 2)))
        cv2.putText(img, "A", ((sz - tw) // 2, (sz + th) // 2), cv2.FONT_HERSHEY_DUPLEX, scale,
                    (24, 18, 11, 255), max(1, round(scale * 2)), cv2.LINE_AA)
        entries.append((sz, cv2.imencode(".png", img)[1].tobytes()))
    head = struct.pack("<HHH", 0, 1, len(entries))
    off = 6 + 16 * len(entries)
    dirs, blobs = b"", b""
    for sz, data in entries:
        dirs += struct.pack("<BBBBHHII", sz, sz, 0, 0, 1, 32, len(data), off)
        off += len(data)
        blobs += data
    path.write_bytes(head + dirs + blobs)


def icon_path(kind="ok"):
    p = ROOT / f"icon_{kind}.ico"
    if not p.exists():
        _make_ico(p, COLORS[kind])
    return p


class Tray(threading.Thread):
    def __init__(self, on_event, get_status):
        super().__init__(daemon=True, name="tray")
        self.on_event = on_event          # callable(str): 'open' | 'toggle' | 'exit'
        self.get_status = get_status      # callable() -> dict(running=bool)
        self.hwnd = None
        self.ready = threading.Event()
        self._tip = ""
        self._kind = "ok"
        self._icons = {}

    # -- ciclo de vida
    def run(self):
        self._taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
        for k in COLORS:
            sz = win32api.GetSystemMetrics(win32con.SM_CXSMICON)   # 16 a 100% de escala, 24 a 150%, etc.
            self._icons[k] = win32gui.LoadImage(0, str(icon_path(k)), win32con.IMAGE_ICON, sz, sz, win32con.LR_LOADFROMFILE)
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = CLASS
        wc.lpfnWndProc = {WM_TRAY: self._on_tray, win32con.WM_COMMAND: self._on_cmd, WM_SHOW: self._on_show,
                          win32con.WM_DESTROY: self._on_destroy, self._taskbar_created: self._on_taskbar}
        win32gui.RegisterClass(wc)
        self.hwnd = win32gui.CreateWindow(CLASS, "AutoRelog", 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)
        self._add()
        self.ready.set()
        win32gui.PumpMessages()

    def stop(self):
        if self.hwnd:
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 0))
                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:  # noqa: BLE001
                pass

    # -- icono
    def _nid(self, flags=0, info=None, title=""):
        f = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP | flags
        base = (self.hwnd, 0, f, WM_TRAY, self._icons[self._kind], self._tip[:120])
        return base + ((info, 5000, title, win32gui.NIIF_INFO) if info else ())

    def _add(self):
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, self._nid())

    def update(self, tip, down=False):
        kind = "down" if down else "ok"
        if (tip, kind) != (self._tip, self._kind) and self.hwnd:
            self._tip, self._kind = tip, kind
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, self._nid())
            except Exception:  # noqa: BLE001
                pass

    def balloon(self, title, text):
        if self.hwnd:
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, self._nid(win32gui.NIF_INFO, text[:250], title[:60]))
            except Exception:  # noqa: BLE001
                pass

    # -- mensajes de la ventana oculta
    def _on_tray(self, hwnd, msg, wp, lp):
        if lp in (win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK):
            self.on_event("open")
        elif lp == win32con.WM_RBUTTONUP:
            menu = win32gui.CreatePopupMenu()
            running = bool(self.get_status().get("running"))
            win32gui.AppendMenu(menu, win32con.MF_STRING, ID_OPEN, "Abrir AutoRelog")
            win32gui.AppendMenu(menu, win32con.MF_STRING, ID_TOGGLE, "Detener vigilancia" if running else "Iniciar vigilancia")
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(menu, win32con.MF_STRING, ID_EXIT, "Salir")
            x, y = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(hwnd)  # sin esto el menu no se cierra al hacer click afuera
            win32gui.TrackPopupMenu(menu, win32con.TPM_LEFTALIGN | win32con.TPM_RIGHTBUTTON, x, y, 0, hwnd, None)
            win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
        return 0

    def _on_cmd(self, hwnd, msg, wp, lp):
        self.on_event({ID_OPEN: "open", ID_TOGGLE: "toggle", ID_EXIT: "exit"}.get(wp & 0xFFFF, ""))
        return 0

    def _on_show(self, hwnd, msg, wp, lp):
        self.on_event("open")
        return 0

    def _on_taskbar(self, hwnd, msg, wp, lp):   # el Explorador se reinicio: volver a poner el icono
        self._add()
        return 0

    def _on_destroy(self, hwnd, msg, wp, lp):
        win32gui.PostQuitMessage(0)
        return 0
