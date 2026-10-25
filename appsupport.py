"""Settings file, single instance lock and tray icon image."""
import json
import logging
import os
import socket
import sys
import threading
from logging.handlers import RotatingFileHandler

from PIL import Image, ImageDraw

DEFAULTS = {
    "port": 8080,
    "latency": "Balanced",
    "device": "Default",
    "start_minimized": False,
    "auto_start_server": True,
    "tray_hint_shown": False,
    "first_run_done": False,
    "pin_enabled": True,
    "pin": "",
}
DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "PhoneSpeaker")
PATH = os.path.join(DIR, "settings.json")
LOCK_PORT = 47653  # local only, used to detect a second copy of the app


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg: dict) -> None:
    os.makedirs(DIR, exist_ok=True)
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


class SingleInstance:
    """First copy owns a local port. A second copy asks it to show its window, then exits."""

    def __init__(self) -> None:
        self.sock = None

    def acquire(self) -> bool:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", LOCK_PORT))
        except OSError:
            s.close()
            try:
                with socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=1) as c:
                    c.sendall(b"SHOW")
            except OSError:
                pass
            return False
        s.listen(5)
        self.sock = s
        return True

    def listen(self, on_show) -> None:
        def loop() -> None:
            while True:
                try:
                    conn, _ = self.sock.accept()
                except OSError:
                    return
                with conn:
                    try:
                        if conn.recv(8) == b"SHOW":
                            on_show()
                    except OSError:
                        pass

        threading.Thread(target=loop, daemon=True).start()

    def release(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None


def tray_image() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, 63, 63), radius=14, fill=(59, 130, 246, 255))
    d.polygon([(12, 26), (22, 26), (34, 16), (34, 48), (22, 38), (12, 38)], fill="white")
    d.arc((36, 22, 48, 42), -60, 60, fill="white", width=3)
    d.arc((40, 16, 56, 48), -60, 60, fill="white", width=3)
    return img


# ---------- start with Windows ----------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "PhoneSpeaker"


def _autostart_command() -> str:
    # Works for the packaged exe and for plain python. --minimized sends boot start to the tray.
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")  # no console window at boot
    if os.path.exists(pythonw):
        exe = pythonw
    return f'"{exe}" "{os.path.abspath(sys.argv[0])}" --minimized'


def get_autostart() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except (ImportError, OSError):
        return False


def set_autostart(on: bool) -> None:
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# ---------- log file ----------
def setup_logging() -> None:
    os.makedirs(DIR, exist_ok=True)
    h = RotatingFileHandler(os.path.join(DIR, "phonespeaker.log"), maxBytes=500_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(logging.WARNING)       # aiortc and aiohttp problems
    logging.getLogger("phonespeaker").setLevel(logging.INFO)