"""PhoneSpeaker - Stage 2: CustomTkinter window.

Run:  python gui.py
The window is only a remote control. The engine in phonespeaker.py does the real work.
"""
import asyncio
import logging
import os
import queue
import secrets
import sys
import threading
import time
from tkinter import messagebox

import customtkinter as ctk
import pystray
import qrcode

import appsupport as sup
import phonespeaker as ps

PRESETS = {"Low": 5, "Balanced": 10, "Stable": 20}  # buffer size in 20 ms frames
GREEN, YELLOW, RED, GRAY = "#4ade80", "#facc15", "#f87171", "#9ca3af"


class Engine:
    """Runs the server in its own thread with its own asyncio loop."""

    def __init__(self) -> None:
        self.thread = None
        self.loop = None
        self.stop_ev = None

    @property
    def alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self, port: int) -> None:
        if self.alive:
            return

        def runner() -> None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.stop_ev = asyncio.Event()
            try:
                self.loop.run_until_complete(ps.run_server(port, self.stop_ev))
            except OSError as e:
                logging.getLogger("phonespeaker").exception("Server could not start")
                if getattr(e, "winerror", None) in (10048, 10013):
                    ps.emit("error", f"Port {port} is already in use. Close the other app or choose another port in Settings.")
                else:
                    ps.emit("error", str(e))
            except Exception as e:  # noqa: BLE001
                logging.getLogger("phonespeaker").exception("Engine crashed")
                ps.emit("error", str(e))
            finally:
                self.loop.close()

        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.alive and self.loop and self.stop_ev:
            self.loop.call_soon_threadsafe(self.stop_ev.set)

    def stop_and_wait(self) -> None:
        self.stop()
        if self.thread:
            self.thread.join(timeout=3)


def browser_name(ua: str) -> str:
    names = (("SamsungBrowser", "Samsung Internet"), ("Firefox", "Firefox"), ("Edg", "Edge"),
             ("CriOS", "Chrome"), ("Chrome", "Chrome"), ("Safari", "Safari"))
    browser = next((n for k, n in names if k in ua), "Browser")
    device = "Android" if "Android" in ua else "iPhone" if "iPhone" in ua else ""
    return f"{browser} on {device}" if device else browser


class App(ctk.CTk):
    def __init__(self, single: sup.SingleInstance) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("PhoneSpeaker")
        self.geometry("440x800")
        self.resizable(False, False)

        self.single = single
        self.closing = False  # set while quitting so late events never touch destroyed widgets
        self.cfg = sup.load()
        self.port = int(self.cfg["port"])
        self.engine = Engine()
        self.running = False
        self.ip = ps.local_ip()
        self.level_shown = 0.0
        self.connected_at = None
        self.agent = ""
        self.last_ip_check = time.monotonic()

        self._build()
        self._build_settings()
        if self.cfg["pin_enabled"] and not self.cfg["pin"]:
            self.cfg["pin"] = self.new_pin()  # first run: a random PIN, safe by default
            sup.save(self.cfg)
            self.pin_entry.insert(0, self.cfg["pin"])
        self.apply_engine_settings()
        self._refresh_link()
        self._start_tray()
        if sup.get_autostart():
            sup.set_autostart(True)  # refresh the stored path in case the app folder was moved
        if not self.cfg["first_run_done"] and not (self.cfg["start_minimized"] or "--minimized" in sys.argv):
            self.after(800, self.first_run_note)
        self.single.listen(lambda: ps.emit("cmd", "open"))
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)  # X hides, Quit in the tray stops
        if self.cfg["auto_start_server"]:
            self.engine.start(self.port)
        else:
            self.set_server(False)
        if self.cfg["start_minimized"] or "--minimized" in sys.argv:
            self.withdraw()
        self.after(100, self.tick)

    # ---------- layout ----------
    def _card(self) -> ctk.CTkFrame:
        card = ctk.CTkFrame(self.main, corner_radius=14)
        card.pack(fill="x", padx=4, pady=(0, 10))
        return card

    def _build(self) -> None:
        ctk.CTkLabel(self, text="PhoneSpeaker", font=ctk.CTkFont(size=24, weight="bold")).pack(pady=(14, 6))
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.main = self.tabs.add("Main")
        self.settings_tab = self.tabs.add("Settings")

        # server card
        c = self._card()
        self.server_dot = ctk.CTkLabel(c, text="●", text_color=GRAY, font=ctk.CTkFont(size=20))
        self.server_dot.pack(side="left", padx=(14, 6), pady=12)
        self.server_lbl = ctk.CTkLabel(c, text="Server starting...")
        self.server_lbl.pack(side="left")
        self.toggle_btn = ctk.CTkButton(c, text="Stop", width=80, command=self.toggle_server)
        self.toggle_btn.pack(side="right", padx=14)

        # link + qr card
        c = self._card()
        ctk.CTkLabel(c, text="Open this link on your phone", anchor="w").pack(fill="x", padx=14, pady=(10, 4))
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=14)
        self.link_entry = ctk.CTkEntry(row)
        self.link_entry.pack(side="left", fill="x", expand=True)
        self.copy_btn = ctk.CTkButton(row, text="Copy", width=70, command=self.copy_link)
        self.copy_btn.pack(side="left", padx=(8, 0))
        self.pin_lbl = ctk.CTkLabel(c, text="", anchor="w", font=ctk.CTkFont(size=16, weight="bold"))
        self.pin_lbl.pack(fill="x", padx=14, pady=(6, 0))
        self.qr_lbl = ctk.CTkLabel(c, text="")
        self.qr_lbl.pack(pady=10)

        # device card
        c = self._card()
        ctk.CTkLabel(c, text="Phone", anchor="w").pack(fill="x", padx=14, pady=(10, 0))
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=14)
        self.phone_dot = ctk.CTkLabel(row, text="●", text_color=GRAY, font=ctk.CTkFont(size=20))
        self.phone_dot.pack(side="left")
        self.phone_lbl = ctk.CTkLabel(row, text="Waiting for phone")
        self.phone_lbl.pack(side="left", padx=6)
        self.phone_info = ctk.CTkLabel(c, text="", anchor="w", text_color=GRAY)
        self.phone_info.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkLabel(c, text="Audio level", anchor="w").pack(fill="x", padx=14)
        self.meter = ctk.CTkProgressBar(c)
        self.meter.set(0)
        self.meter.pack(fill="x", padx=14, pady=(2, 14))

        # log
        self.log_box = ctk.CTkTextbox(self.main, height=120, state="disabled")
        self.log_box.pack(fill="both", expand=True, padx=4, pady=(0, 4))

    # ---------- actions ----------
    @property
    def link(self) -> str:
        return f"http://{self.ip}:{self.port}"

    def _refresh_link(self) -> None:
        self.link_entry.configure(state="normal")
        self.link_entry.delete(0, "end")
        self.link_entry.insert(0, self.link)
        self.link_entry.configure(state="readonly")
        pin = self.cfg["pin"] if self.cfg["pin_enabled"] else ""
        self.pin_lbl.configure(text=f"PIN: {pin}" if pin else "PIN: off")
        # The QR carries the PIN after the # sign, so scanning needs no typing. The copied text link has no PIN.
        img = qrcode.make(f"{self.link}#{pin}" if pin else self.link).get_image().convert("RGB").resize((170, 170))
        self.qr_img = ctk.CTkImage(light_image=img, dark_image=img, size=(170, 170))  # keep a reference
        self.qr_lbl.configure(image=self.qr_img)

    def copy_link(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.link)
        self.copy_btn.configure(text="Copied")
        self.after(1500, lambda: self.copy_btn.configure(text="Copy"))

    def toggle_server(self) -> None:
        self.toggle_btn.configure(state="disabled")  # enabled again when the server event arrives
        if self.running:
            self.engine.stop()
        else:
            self.engine.start(self.port)

    def add_log(self, line: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ---------- events from the engine ----------
    def set_server(self, up: bool) -> None:
        self.running = up
        self.server_dot.configure(text_color=GREEN if up else GRAY)
        self.server_lbl.configure(text="Server running" if up else "Server stopped")
        self.toggle_btn.configure(text="Stop" if up else "Start", state="normal")
        if up:
            self.set_phone("waiting", "")
        else:
            self.set_phone("off", "")

    def set_phone(self, state: str, agent: str) -> None:
        self.phone_info.configure(text_color=GRAY)
        if agent:
            self.agent = browser_name(agent)
        if state == "connected":
            self.connected_at = time.monotonic()
            dot, txt = GREEN, "Phone connected"
        elif state in ("new", "connecting"):
            dot, txt = YELLOW, "Connecting..."
        elif state in ("failed", "closed", "disconnected"):
            self.connected_at = None
            dot, txt = RED, "Phone disconnected"
        elif state == "waiting":
            self.connected_at, self.agent = None, ""
            dot, txt = YELLOW, "Waiting for phone"
        else:  # server off
            self.connected_at, self.agent = None, ""
            dot, txt = GRAY, "Server is off"
        self.phone_dot.configure(text_color=dot)
        self.phone_lbl.configure(text=txt)
        if state != "connected":
            self.phone_info.configure(text=self.agent if state in ("new", "connecting") else "")

    def handle(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "log":
            self.add_log(ev[1])
        elif kind == "error":
            self.add_log("ERROR: " + ev[1])
            logging.getLogger("phonespeaker").error(ev[1])
            self.server_lbl.configure(text="Server error")
            self.toggle_btn.configure(text="Start", state="normal")
            self.running = False
        elif kind == "warn":  # the server is fine, only audio had a problem
            self.add_log("WARNING: " + ev[1])
            self.phone_info.configure(text=ev[1], text_color=RED)
        elif kind == "server":
            self.set_server(ev[1])
        elif kind == "phone":
            self.set_phone(ev[1], ev[2] if len(ev) > 2 else "")
        elif kind == "cmd":
            self.command(ev[1])

    # ---------- polling loop, the only place that touches widgets from engine data ----------
    def tick(self) -> None:
        try:
            while not self.closing:
                self.handle(ps.events.get_nowait())
        except queue.Empty:
            pass
        if self.closing:  # Quit was pressed inside handle, the window is gone, so stop here
            return

        # audio meter with a slow fall so it looks smooth
        silent = time.monotonic() - ps.cap.last_cb > 0.3
        target = 0.0 if silent else min(1.0, (ps.cap.level ** 0.5) * 2)
        self.level_shown = max(target, self.level_shown * 0.8)
        self.meter.set(self.level_shown)

        if self.connected_at:
            secs = int(time.monotonic() - self.connected_at)
            info = f"{self.agent}  |  connected {secs // 60:02d}:{secs % 60:02d}"
            self.phone_info.configure(text=info.strip(" |"))

        # Wi-Fi may change, so refresh the link and QR when the IP changes
        if time.monotonic() - self.last_ip_check > 5:
            self.last_ip_check = time.monotonic()
            new_ip = ps.local_ip()
            if new_ip != self.ip:
                self.ip = new_ip
                self._refresh_link()
                self.add_log(f"Wi-Fi IP changed, new link: {self.link}")

        self.after(100, self.tick)

    # ---------- settings tab ----------
    def _build_settings(self) -> None:
        t = ctk.CTkScrollableFrame(self.settings_tab, fg_color="transparent")
        t.pack(fill="both", expand=True)

        def label(text: str) -> None:
            ctk.CTkLabel(t, text=text, anchor="w").pack(fill="x", padx=12, pady=(12, 2))

        label("Port")
        self.port_entry = ctk.CTkEntry(t)
        self.port_entry.insert(0, str(self.port))
        self.port_entry.pack(fill="x", padx=12)

        label("Audio output to capture")
        devices = ["Default"] + ps.list_loopbacks()
        self.device_menu = ctk.CTkOptionMenu(t, values=devices)
        self.device_menu.set(self.cfg["device"] if self.cfg["device"] in devices else "Default")
        self.device_menu.pack(fill="x", padx=12)

        label("Latency")
        self.latency_btn = ctk.CTkSegmentedButton(t, values=list(PRESETS))
        self.latency_btn.set(self.cfg["latency"] if self.cfg["latency"] in PRESETS else "Balanced")
        self.latency_btn.pack(fill="x", padx=12)

        self.pin_sw = ctk.CTkSwitch(t, text="Require PIN on phone")
        self.pin_sw.pack(anchor="w", padx=12, pady=(14, 4))
        if self.cfg["pin_enabled"]:
            self.pin_sw.select()
        row = ctk.CTkFrame(t, fg_color="transparent")
        row.pack(fill="x", padx=12)
        self.pin_entry = ctk.CTkEntry(row, placeholder_text="4 to 8 digits")
        self.pin_entry.insert(0, self.cfg["pin"])
        self.pin_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="Random", width=70, command=self.random_pin).pack(side="left", padx=(8, 0))

        self.min_sw = ctk.CTkSwitch(t, text="Start minimized to tray")
        self.auto_sw = ctk.CTkSwitch(t, text="Start server when app opens")
        for sw, key in ((self.min_sw, "start_minimized"), (self.auto_sw, "auto_start_server")):
            sw.pack(anchor="w", padx=12, pady=(14, 0))
            if self.cfg[key]:
                sw.select()

        self.boot_sw = ctk.CTkSwitch(t, text="Start with Windows")
        self.boot_sw.pack(anchor="w", padx=12, pady=(14, 0))
        if sup.get_autostart():  # the registry is the truth, not the settings file
            self.boot_sw.select()

        ctk.CTkButton(t, text="Save", command=self.save_settings).pack(fill="x", padx=12, pady=(20, 6))
        ctk.CTkButton(t, text="Open log folder", fg_color="transparent", border_width=1,
                      command=lambda: os.startfile(sup.DIR)).pack(fill="x", padx=12, pady=(0, 6))
        self.settings_msg = ctk.CTkLabel(t, text="")
        self.settings_msg.pack()

    @staticmethod
    def new_pin() -> str:
        return f"{secrets.randbelow(10000):04d}"

    def random_pin(self) -> None:
        self.pin_entry.delete(0, "end")
        self.pin_entry.insert(0, self.new_pin())

    def apply_engine_settings(self) -> None:
        ps.PIN = self.cfg["pin"] if self.cfg["pin_enabled"] else ""
        ps.cap.max_buffer = ps.FRAME_BYTES * PRESETS.get(self.cfg["latency"], 10)
        ps.cap.device_name = "" if self.cfg["device"] == "Default" else self.cfg["device"]

    def save_settings(self) -> None:
        try:
            port = int(self.port_entry.get())
            if not 1024 <= port <= 65535:
                raise ValueError
        except ValueError:
            self.settings_msg.configure(text="Port must be a number from 1024 to 65535", text_color=RED)
            return
        pin, pin_on = self.pin_entry.get().strip(), bool(self.pin_sw.get())
        if pin_on and not (pin.isdigit() and 4 <= len(pin) <= 8):
            self.settings_msg.configure(text="PIN must be 4 to 8 digits", text_color=RED)
            return
        self.cfg.update(
            port=port, device=self.device_menu.get(), latency=self.latency_btn.get(),
            start_minimized=bool(self.min_sw.get()), auto_start_server=bool(self.auto_sw.get()),
            pin_enabled=pin_on, pin=pin or self.cfg["pin"],
        )
        sup.save(self.cfg)
        self.apply_engine_settings()
        boot_err = ""
        try:
            sup.set_autostart(bool(self.boot_sw.get()))
        except Exception as e:  # noqa: BLE001
            boot_err = f" Could not change Start with Windows: {e}"
        msg = "Saved. A new audio device applies on the next phone connection."
        if port != self.port:
            self.port = port
            if self.running:
                self.engine.stop_and_wait()
                self.engine.start(port)
            self._refresh_link()
            msg = "Saved. The link has changed, scan the new QR code."
        self._refresh_link()
        self.settings_msg.configure(text=msg + boot_err, text_color=RED if boot_err else GREEN, wraplength=360)

    # ---------- tray and window ----------
    def _start_tray(self) -> None:
        item = pystray.MenuItem
        # Tray callbacks run in the tray thread, so they only post a command to the queue.
        menu = pystray.Menu(
            item("Open", lambda: ps.emit("cmd", "open"), default=True),
            item(lambda i: "Stop server" if self.running else "Start server", lambda: ps.emit("cmd", "toggle")),
            item("Copy phone link", lambda: ps.emit("cmd", "copy")),
            item("Quit", lambda: ps.emit("cmd", "quit")),
        )
        self.tray = pystray.Icon("PhoneSpeaker", sup.tray_image(), "PhoneSpeaker", menu)
        self.tray.run_detached()

    def command(self, name: str) -> None:
        if name == "open":
            self.show_window()
        elif name == "toggle":
            self.toggle_server()
        elif name == "copy":
            self.copy_link()
        elif name == "quit":
            self.quit_app()

    def show_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def hide_to_tray(self) -> None:
        self.withdraw()
        if not self.cfg["tray_hint_shown"]:
            self.tray.notify("PhoneSpeaker is still running in the background. Use the tray icon to open or quit.", "PhoneSpeaker")
            self.cfg["tray_hint_shown"] = True
            sup.save(self.cfg)

    def first_run_note(self) -> None:
        messagebox.showinfo("PhoneSpeaker", "The first time, Windows Firewall may ask for permission. "
                            "Please allow access on Private networks, otherwise your phone cannot open the link.")
        self.cfg["first_run_done"] = True
        sup.save(self.cfg)

    def report_callback_exception(self, exc, val, tb) -> None:
        logging.getLogger("phonespeaker").error("UI error", exc_info=(exc, val, tb))
        if not self.closing:
            try:
                self.add_log(f"UI error: {val}")
            except Exception:  # noqa: BLE001
                pass

    def quit_app(self) -> None:
        self.closing = True
        self.engine.stop_and_wait()
        self.tray.stop()
        self.single.release()
        self.destroy()


if __name__ == "__main__":
    sup.setup_logging()
    single = sup.SingleInstance()
    if single.acquire():  # a second copy only wakes up the first one and exits
        App(single).mainloop()
