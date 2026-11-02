"""SAOUB Launcher: start, stop, update and manage a local SAO Unleash Blading private server (Windows first, tkinter).

usage: SAOUB_Launcher.exe (standalone bundle, tools/launcher/build_bundle.py)
   or: double-click SAOUB_Launcher.bat at the repository root (.venv/Scripts/pythonw tools/launcher/saoub_launcher.pyw)

What it does
  - Start / Stop server/main.py in a hidden child process (Stop terminates it; closing the window asks first).
  - Status: process alive + the game API answers (GET /gcs/.../maintenance.json, served without game headers).
  - Settings: game data folder (SAOUB_DATA), database file (SAOUB_DB), game port, admin panel port and password.
    Kept in %APPDATA%/SAOUB Launcher/settings.json (outside the repository); the admin password goes to
    server/admin_password.txt like start_local_server.bat (gitignored).
  - Admin panel, logs folder, database backup (sqlite3 online backup, safe while the server runs).
  - Connect: the server URL for BlueStacks (10.0.2.2) and for phones on the same Wi-Fi (this PC's LAN address),
    and installs an APK into BlueStacks with its own adb (HD-Adb.exe).
  - Frame rate (SETTINGS): 60 / 90 / 120 fps for APKs built with tools/fps_patch.py, written to BlueStacks as
    Android/data/com.bandainamcoent.saoars/files/saoub_fps.txt (read when the game starts).
  - Update: signed updates published by the owner (tools/launcher/updater.py, publish_update.py): server code,
    data pack, game APK. Disabled in a git checkout (developers use git).
The APK itself must already point to the URL (tools/patch_apk.py / tools/build_apk.sh); the launcher does not
rebuild or sign APKs (that needs the JDK and Android build tools).
"""
import datetime
import hashlib
import json
import os
import queue
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import updater  # noqa: E402

ROOT = updater.ROOT
SERVER_DIR = os.path.join(ROOT, "server")
DOWNLOADS = os.path.join(ROOT, "downloads")
ADMIN_PASSWORD_FILE = os.path.join(SERVER_DIR, "admin_password.txt")
# standalone build (SAOUB_Launcher.exe, tools/launcher/boot.py): the exe also runs the server (--server)
FROZEN = getattr(sys, "frozen", False)
# one settings file per install folder (a player may keep two installs)
SETTINGS_FILE = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "SAOUB Launcher",
                             "settings_" + hashlib.md5(os.path.normcase(ROOT).encode()).hexdigest()[:8] + ".json")
BLUESTACKS_ADB = os.environ.get("SAOUB_ADB", r"C:\Program Files\BlueStacks_nxt\HD-Adb.exe")
BLUESTACKS_DEVICE = "127.0.0.1:5555"
# frame-rate setting read by the patched game at start (tools/fps_patch/Fps.java)
FPS_FILE = "/sdcard/Android/data/com.bandainamcoent.saoars/files/saoub_fps.txt"
FPS_CHOICES = {"60 fps (game default)": 0, "90 fps": 90, "120 fps": 120}
MAX_LOG_LINES = 2000
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TITLE = "SAOUB Launcher"

# SAO UB look: dark navy, cyan system lines, gold highlights
BG, PANEL, BORDER = "#0a0f1c", "#121a2c", "#24365a"
CYAN, GOLD, TEXT, MUTED = "#4fd2ff", "#f0b445", "#e8f0ff", "#8a9bb8"
GREEN, RED, ORANGE = "#5be3a0", "#ff5a6a", "#ffb34f"
FONT = "Bahnschrift"


def default_data_dir() -> str:
    """<install>/game (standalone bundle, every game file), then the lookup of server/config.py: <repo>/dev_data
    when present, else <repo>/data (built from your own copy of the game, see README.md)."""
    for name in ("game", "dev_data"):
        d = os.path.join(ROOT, name)
        if os.path.isdir(os.path.join(d, "server_cache")):
            return d
    return os.path.join(ROOT, "data")


def default_apk() -> str:
    """Newest APK in <install>/downloads (the bundle ships the BlueStacks APK there), or ""."""
    try:
        apks = [os.path.join(DOWNLOADS, f) for f in os.listdir(DOWNLOADS) if f.lower().endswith(".apk")]
    except OSError:
        return ""
    return max(apks, key=os.path.getmtime) if apks else ""


def server_command() -> list:
    """Command line of the game server, run in SERVER_DIR."""
    if FROZEN:
        return [sys.executable, "--server"]
    return [server_python(), "-u", "main.py"]


def server_python() -> str:
    """python.exe (not pythonw) of the repository's venv, else the one running the launcher."""
    for cand in (os.path.join(ROOT, ".venv", "Scripts", "python.exe"), os.path.join(ROOT, ".venv", "bin", "python")):
        if os.path.exists(cand):
            return cand
    exe = sys.executable
    if os.path.basename(exe).lower() == "pythonw.exe":
        exe = os.path.join(os.path.dirname(exe), "python.exe")
    return exe


def lan_address() -> str:
    """This PC's IPv4 address on the local network (what a phone on the same Wi-Fi connects to), or ""."""
    try:
        # no packet is sent: connect() on UDP only picks the interface of the default route (not VM adapters)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.1.1", 9))
            addr = s.getsockname()[0]
    except OSError:
        return ""
    return "" if addr.startswith(("127.", "169.254.", "0.")) else addr


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def api_answers(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/gcs/ap/maintenance.json", timeout=2) as r:
            return r.status == 200 and "valid" in json.loads(r.read(4096))
    except Exception:
        return False


def server_alive(port: int) -> bool:
    """Lighter check once the API answered: a non-game path, which the server logs only once (not every poll)."""
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/saoub-launcher-ping", timeout=2)
        return True
    except urllib.error.HTTPError:
        return True  # 404 "not found": the server answers
    except Exception:
        return False


class SaoButton(tk.Canvas):
    """Chamfered SAO-style button: dark plate, coloured outline, lighter plate on hover."""

    def __init__(self, parent, text, command, width=170, height=38, accent=CYAN, big=False):
        super().__init__(parent, width=width, height=height, bg=parent["bg"], highlightthickness=0, cursor="hand2")
        self.command, self.accent, self.enabled = command, accent, True
        c = 10
        pts = (c, 1, width - 1, 1, width - 1, height - c, width - c, height - 1, 1, height - 1, 1, c)
        self.plate = self.create_polygon(pts, fill=PANEL, outline=accent, width=1.5)
        self.label = self.create_text(width / 2, height / 2, text=text, fill=TEXT,
                                      font=(FONT, 14 if big else 10, "bold" if big else "normal"))
        self.bind("<Enter>", lambda e: self.enabled and self.itemconfigure(self.plate, fill=BORDER))
        self.bind("<Leave>", lambda e: self.itemconfigure(self.plate, fill=PANEL))
        self.bind("<Button-1>", lambda e: self.enabled and self.command())

    def set(self, text=None, enabled=None, accent=None):
        if text is not None:
            self.itemconfigure(self.label, text=text)
        if accent is not None:
            self.accent = accent
        if enabled is not None:
            self.enabled = enabled
            self.configure(cursor="hand2" if enabled else "arrow")
        self.itemconfigure(self.plate, outline=self.accent if self.enabled else BORDER)
        self.itemconfigure(self.label, fill=TEXT if self.enabled else MUTED)


class Launcher:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.proc = None
        self.lines = queue.Queue()
        self.ui = queue.Queue()  # callables from worker threads, run on the Tk thread
        self.api_ok = False
        self.busy = False
        self.manifest = None
        self.settings = self.load_settings()
        root.title(f"{TITLE} - SAO Unleash Blading private server")
        root.geometry("900x620")
        root.minsize(760, 520)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.build_ui()
        self.show("home")
        self.refresh_status()
        self.root.after(100, self.pump)
        if updater.config().get("url") and not updater.developer_checkout():
            self.check_update(quiet=True)

    # ----- settings -----

    def load_settings(self) -> dict:
        s = {"data_dir": default_data_dir(), "db": os.path.join(SERVER_DIR, "saoub.sqlite3"),
             "port": 8080, "admin_port": 8082, "apk": ""}
        s.update(updater.load_json(SETTINGS_FILE, {}))
        if not s["apk"] or not os.path.exists(s["apk"]):
            s["apk"] = default_apk()
        return s

    def save_settings(self):
        self.settings.update(data_dir=self.var_data.get().strip(), db=self.var_db.get().strip(),
                             port=self.int_var(self.var_port, 8080), admin_port=self.int_var(self.var_admin_port, 8082),
                             apk=self.var_apk.get().strip(), fps=FPS_CHOICES.get(self.var_fps.get(), 0))
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, indent=2)
        password = self.var_password.get().strip()
        if password:
            with open(ADMIN_PASSWORD_FILE, "w", encoding="utf-8") as f:
                f.write(password + "\n")
        elif os.path.exists(ADMIN_PASSWORD_FILE):
            os.remove(ADMIN_PASSWORD_FILE)

    @staticmethod
    def int_var(var: tk.StringVar, default: int) -> int:
        try:
            return int(var.get())
        except ValueError:
            return default

    @staticmethod
    def read_password() -> str:
        try:
            with open(ADMIN_PASSWORD_FILE, encoding="utf-8-sig") as f:
                return f.readline().strip()
        except OSError:
            return ""

    # ----- UI -----

    def label(self, parent, text, size=10, color=TEXT, bold=False, **kw):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=color, font=(FONT, size, "bold" if bold else "normal"),
                        **kw)

    def entry(self, parent, var, width=50, show=""):
        return tk.Entry(parent, textvariable=var, width=width, show=show, bg=PANEL, fg=TEXT, insertbackground=CYAN,
                        relief="flat", highlightthickness=1, highlightbackground=BORDER, highlightcolor=CYAN,
                        font=(FONT, 10))

    def section(self, parent, title):
        self.label(parent, title.upper(), 9, CYAN, bold=True).pack(anchor="w", pady=(14, 2))
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", pady=(0, 8))

    def build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Sao.Horizontal.TProgressbar", troughcolor=PANEL, background=CYAN, bordercolor=BORDER,
                        lightcolor=CYAN, darkcolor=CYAN)

        # header: title + status
        head = tk.Canvas(self.root, height=92, bg=BG, highlightthickness=0)
        head.pack(fill="x")
        head.create_text(24, 22, text="S W O R D   A R T   O N L I N E", anchor="w", fill=CYAN, font=(FONT, 9))
        head.create_text(22, 50, text="UNLEASH BLADING", anchor="w", fill=TEXT, font=(FONT, 24, "bold"))
        head.create_text(25, 78, text="PRIVATE SERVER LAUNCHER", anchor="w", fill=GOLD, font=(FONT, 9, "bold"))
        head.create_text(205, 78, text="•  32 / 64-BIT SUPPORTED  •  UP TO 120 FPS", anchor="w", fill=CYAN,
                         font=(FONT, 9, "bold"))
        head.create_line(0, 91, 4000, 91, fill=BORDER)
        head.create_line(0, 91, 260, 91, fill=CYAN, width=2)
        self.status_dot = head.create_oval(0, 0, 0, 0, fill=MUTED, outline="")
        self.status_text = head.create_text(0, 0, text="", anchor="e", fill=TEXT, font=(FONT, 12, "bold"))
        self.head = head
        head.bind("<Configure>", lambda e: self.place_status())

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True)
        menu = tk.Frame(body, bg=BG, width=190)
        menu.pack(side="left", fill="y", padx=(16, 8), pady=12)
        self.pages, self.menu_buttons = {}, {}
        holder = tk.Frame(body, bg=BG)
        holder.pack(side="left", fill="both", expand=True, padx=(8, 18), pady=4)
        for key, text in (("home", "HOME"), ("settings", "SETTINGS"), ("connect", "CONNECT THE GAME"),
                          ("logs", "SERVER LOG"), ("update", "UPDATE")):
            b = SaoButton(menu, text, lambda k=key: self.show(k))
            b.pack(pady=4)
            self.menu_buttons[key] = b
            self.pages[key] = tk.Frame(holder, bg=BG)
        self.footer = self.label(menu, "", 8, MUTED, justify="left")
        self.footer.pack(side="bottom", anchor="w")

        self.var_data = tk.StringVar(value=self.settings["data_dir"])
        self.var_db = tk.StringVar(value=self.settings["db"])
        self.var_port = tk.StringVar(value=str(self.settings["port"]))
        self.var_admin_port = tk.StringVar(value=str(self.settings["admin_port"]))
        self.var_password = tk.StringVar(value=self.read_password())
        self.var_apk = tk.StringVar(value=self.settings.get("apk", ""))
        self.var_fps = tk.StringVar(value=next((k for k, v in FPS_CHOICES.items() if v == self.settings.get("fps", 0)),
                                               next(iter(FPS_CHOICES))))
        self.build_home(self.pages["home"])
        self.build_settings(self.pages["settings"])
        self.build_connect(self.pages["connect"])
        self.build_logs(self.pages["logs"])
        self.build_update(self.pages["update"])
        self.update_footer()

    def place_status(self):
        w = self.head.winfo_width()
        self.head.coords(self.status_text, w - 28, 50)
        self.head.coords(self.status_dot, w - 20, 45, w - 10, 55)

    def show(self, key):
        for k, page in self.pages.items():
            page.pack_forget()
            self.menu_buttons[k].set(accent=GOLD if k == key else CYAN)
        self.pages[key].pack(fill="both", expand=True)
        if key == "connect":
            self.fill_connect_text()

    def build_home(self, p):
        self.section(p, "Server")
        row = tk.Frame(p, bg=BG)
        row.pack(anchor="w")
        self.btn_start = SaoButton(row, "START SERVER", self.start, width=230, height=56, accent=GREEN, big=True)
        self.btn_start.pack(side="left", padx=(0, 12))
        self.btn_stop = SaoButton(row, "STOP", self.stop, width=150, height=56, accent=RED, big=True)
        self.btn_stop.pack(side="left")
        self.home_info = self.label(p, "", 10, MUTED, justify="left")
        self.home_info.pack(anchor="w", pady=(10, 0))
        if not FROZEN:  # the standalone bundle already ships its game data
            self.section(p, "Game data")
            SaoButton(p, "DOWNLOAD GAME DATA", self.download_data, width=260, accent=GOLD).pack(anchor="w")
            self.label(p, "Fetches the game data from the Internet Archive (~5 GB, ~10 GB free space needed) and builds "
                          "it into the game data folder (SETTINGS). Resumes if interrupted.", 9, MUTED, justify="left",
                       wraplength=560).pack(anchor="w", pady=(6, 0))
        self.section(p, "Tools")
        row = tk.Frame(p, bg=BG)
        row.pack(anchor="w")
        for i, (text, cmd) in enumerate((("Admin panel", self.open_admin), ("Back up database", self.backup),
                                         ("Logs folder", self.open_logs), ("Backups folder", self.open_backups))):
            SaoButton(row, text, cmd, width=150).grid(row=0, column=i, padx=(0, 8))
        self.update_badge = self.label(p, "", 10, GOLD, cursor="hand2")
        self.update_badge.pack(anchor="w", pady=(16, 0))
        self.update_badge.bind("<Button-1>", lambda e: self.show("update"))

    def build_settings(self, p):
        self.section(p, "Game data")
        for label, var, cmd in (("Game data folder (contains server_cache)", self.var_data, self.pick_data),
                                ("Database file (your accounts)", self.var_db, self.pick_db)):
            self.label(p, label, 9, MUTED).pack(anchor="w")
            row = tk.Frame(p, bg=BG)
            row.pack(fill="x", pady=(0, 8))
            self.entry(row, var).pack(side="left", fill="x", expand=True, ipady=4)
            SaoButton(row, "Browse", cmd, width=90, height=30).pack(side="left", padx=(8, 0))
        self.section(p, "Network and admin panel")
        grid = tk.Frame(p, bg=BG)
        grid.pack(anchor="w")
        for i, (label, var, width, show) in enumerate((("Game port", self.var_port, 8, ""),
                                                       ("Admin panel port", self.var_admin_port, 8, ""),
                                                       ("Admin password", self.var_password, 24, "*"))):
            self.label(grid, label, 10).grid(row=i, column=0, sticky="w", pady=3, padx=(0, 12))
            self.entry(grid, var, width, show).grid(row=i, column=1, sticky="w", pady=3, ipady=3)
        self.label(p, "Empty password = admin panel off. Changes apply at the next server start.", 9, MUTED).pack(
            anchor="w", pady=(6, 0))
        self.section(p, "Frame rate (BlueStacks)")
        fps_menu = tk.OptionMenu(p, self.var_fps, *FPS_CHOICES)
        fps_menu.configure(bg=PANEL, fg=TEXT, activebackground=BORDER, activeforeground=TEXT, relief="flat",
                           highlightthickness=1, highlightbackground=BORDER, font=(FONT, 10))
        fps_menu["menu"].configure(bg=PANEL, fg=TEXT, activebackground=CYAN, activeforeground=BG, font=(FONT, 10))
        fps_menu.pack(anchor="w")
        self.label(p, "Needs an APK with the frame-rate patch (tools/fps_patch.py) and, in BlueStacks, Settings > "
                      "Performance > frame rate 120 + high frame rate on. Saved to BlueStacks when you click SAVE or "
                      "install the APK; restart the game to apply.", 9, MUTED, justify="left", wraplength=560).pack(
            anchor="w", pady=(6, 0))
        SaoButton(p, "SAVE", self.save_clicked, width=120, accent=GOLD).pack(anchor="w", pady=12)

    def build_connect(self, p):
        self.section(p, "Server address for the game")
        self.connect_text = tk.Text(p, height=16, wrap="word", relief="flat", bg=BG, fg=TEXT, font=(FONT, 10),
                                    highlightthickness=0)
        self.connect_text.tag_configure("url", foreground=CYAN, font=(FONT, 11, "bold"))
        self.connect_text.pack(fill="x")
        self.section(p, "Install the game in BlueStacks")
        row = tk.Frame(p, bg=BG)
        row.pack(fill="x")
        self.entry(row, self.var_apk).pack(side="left", fill="x", expand=True, ipady=4)
        SaoButton(row, "Browse", self.pick_apk, width=90, height=30).pack(side="left", padx=(8, 0))
        SaoButton(p, "INSTALL INTO BLUESTACKS", self.install_apk, width=240, accent=GOLD).pack(anchor="w", pady=10)

    def build_logs(self, p):
        self.section(p, "Server log (live)")
        frame = tk.Frame(p, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        frame.pack(fill="both", expand=True, pady=(0, 12))
        self.log = tk.Text(frame, wrap="none", font=("Consolas", 9), state="disabled", bg=PANEL, fg=TEXT,
                           relief="flat", insertbackground=CYAN)
        scroll = tk.Scrollbar(frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)

    def build_update(self, p):
        self.section(p, "Updates")
        self.update_info = self.label(p, "", 10, TEXT, justify="left", wraplength=600)
        self.update_info.pack(anchor="w")
        self.update_notes = self.label(p, "", 10, MUTED, justify="left", wraplength=600)
        self.update_notes.pack(anchor="w", pady=(8, 0))
        self.progress = ttk.Progressbar(p, style="Sao.Horizontal.TProgressbar", length=420, maximum=1000)
        self.progress.pack(anchor="w", pady=12)
        self.progress_text = self.label(p, "", 9, MUTED)
        self.progress_text.pack(anchor="w")
        row = tk.Frame(p, bg=BG)
        row.pack(anchor="w", pady=10)
        self.btn_check = SaoButton(row, "CHECK FOR UPDATES", lambda: self.check_update(), width=200)
        self.btn_check.pack(side="left", padx=(0, 10))
        self.btn_update = SaoButton(row, "UPDATE NOW", self.run_update, width=170, accent=GOLD)
        self.btn_update.pack(side="left")
        self.btn_update.set(enabled=False)
        self.refresh_update_page()

    def update_footer(self):
        self.footer.configure(text=f"launcher v{updater.installed_code_version()}\n"
                                   f"data v{updater.installed_data_version(self.var_data.get().strip())}\n"
                                   "Re:Rising")

    def fill_connect_text(self):
        port = self.int_var(self.var_port, 8080)
        t = self.connect_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("end", "Your game must use a patched APK that points to this server.\n")
        t.insert("end", "32-bit and 64-bit supported: an APK built with the 64-bit game (tools/build_apk.sh with "
                        "ARM64=...) also runs on 64-bit-only phones and emulators (e.g. MuMu on Android 15).\n\n")
        t.insert("end", "BlueStacks / Android emulator on this PC\n")
        t.insert("end", f"    http://10.0.2.2:{port}\n", "url")
        if lan_address():
            t.insert("end", "Phone on the same Wi-Fi (needs an APK made for this address, and the server started "
                            "with SAOUB_HOST=0.0.0.0)\n")
            t.insert("end", f"    http://{lan_address()}:{port}\n", "url")
        t.insert("end", "\n1. HOME > Start server, wait for \"ONLINE\".\n"
                        "2. Install the APK (below, or drag the .apk onto the BlueStacks window). If Android refuses, "
                        "uninstall the original game first.\n"
                        "3. Launch the game: the first launch creates your account and downloads the game files.\n\n"
                        "Phone: allow Python through the Windows firewall (private networks) when Windows asks.")
        t.configure(state="disabled")

    def pick_data(self):
        d = filedialog.askdirectory(initialdir=self.var_data.get() or ROOT, title="Game data folder")
        if d:
            self.var_data.set(d)

    def pick_db(self):
        f = filedialog.asksaveasfilename(initialfile=os.path.basename(self.var_db.get()) or "saoub.sqlite3",
                                         initialdir=os.path.dirname(self.var_db.get()) or SERVER_DIR,
                                         defaultextension=".sqlite3", confirmoverwrite=False,
                                         filetypes=[("SQLite database", "*.sqlite3"), ("All files", "*.*")])
        if f:
            self.var_db.set(f)

    def pick_apk(self):
        f = filedialog.askopenfilename(filetypes=[("Android app", "*.apk"), ("All files", "*.*")])
        if f:
            self.var_apk.set(f)
            self.save_settings()

    def save_clicked(self):
        self.save_settings()
        self.update_footer()
        if os.path.exists(BLUESTACKS_ADB):
            threading.Thread(target=lambda: self.lines.put("adb: " + self.push_fps()), daemon=True).start()
        messagebox.showinfo(TITLE, "Settings saved." + (" Restart the server to apply them." if self.running() else ""))

    # ----- server process -----

    def start(self):
        if self.running() or self.busy:
            return
        port, admin_port = self.int_var(self.var_port, 0), self.int_var(self.var_admin_port, 0)
        data = self.var_data.get().strip()
        if not (0 < port < 65536 and 0 < admin_port < 65536) or port == admin_port:
            return messagebox.showerror(TITLE, "Pick two different ports between 1 and 65535.")
        if not os.path.isdir(os.path.join(data, "server_cache")):
            self.show("settings")
            return messagebox.showerror(TITLE, f"No game data in:\n{data}\n\n"
                                        "Pick the folder that contains 'server_cache' (the data pack).")
        for p in (port, admin_port) if self.var_password.get().strip() else (port,):
            if not port_free(p):
                return messagebox.showerror(TITLE, f"Port {p} is already in use (another server running?). "
                                            "Stop it or pick another port in SETTINGS.")
        self.save_settings()
        env = dict(os.environ, SAOUB_PORT=str(port), SAOUB_ADMIN_PORT=str(admin_port), SAOUB_DATA=data,
                   SAOUB_DB=self.var_db.get().strip(), SAOUB_ADMIN_PASSWORD=self.var_password.get().strip(),
                   SAOUB_LOG_MAX_BYTES=os.environ.get("SAOUB_LOG_MAX_BYTES", "50000000"),
                   PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        # the game files bundled in the APK are served from it: without the original APK at the repository root
        # (standalone bundle), use the patched one (same assets/hashed)
        apk = self.var_apk.get().strip()
        if not any(os.path.exists(p) for p in (os.path.join(data, "game.apk"),
                                               os.path.join(ROOT, "SAO+Unleash+Blading_3.7.0_APKPure.apk"))) \
                and os.path.isfile(apk):
            env["SAOUB_APK"] = apk
        self.append(f"--- starting server on port {port} (data: {data}) ---")
        try:
            self.proc = subprocess.Popen(server_command(), cwd=SERVER_DIR, env=env,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         creationflags=CREATE_NO_WINDOW)
        except OSError as e:
            return messagebox.showerror(TITLE, f"Could not start Python:\n{e}")
        threading.Thread(target=self.read_output, args=(self.proc,), daemon=True).start()
        self.refresh_status()

    def download_data(self):
        """tools/setup_data.py into the game data folder, its progress in SERVER LOG."""
        if getattr(self, "setup_proc", None) and self.setup_proc.poll() is None:
            self.show("logs")
            return
        if self.running():
            return messagebox.showinfo(TITLE, "Stop the server first: the game data folder is being rebuilt.")
        data = self.var_data.get().strip() or os.path.join(ROOT, "data")
        os.makedirs(data, exist_ok=True)
        free = shutil.disk_usage(data).free / 2 ** 30
        if not messagebox.askyesno(TITLE, f"Download the game data (~5 GB) from the Internet Archive and build it in:\n"
                                          f"{data}\n\nAbout 10 GB of free space is needed ({free:.0f} GB free). "
                                          "It can take a while; you can close the launcher and resume later.\n\n"
                                          "Continue?"):
            return
        self.var_data.set(data)
        self.save_settings()
        self.append(f"--- downloading the game data into {data} ---")
        self.show("logs")
        try:
            self.setup_proc = subprocess.Popen(
                [server_python(), "-u", os.path.join(ROOT, "tools", "setup_data.py"), "--data", data], cwd=ROOT,
                env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1"), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
        except OSError as e:
            return messagebox.showerror(TITLE, f"Could not start Python:\n{e}")

        def follow(proc):
            for raw in proc.stdout:
                self.lines.put(raw.decode("utf-8", "replace").rstrip())
            code = proc.wait()
            self.lines.put("--- game data ready: START SERVER ---" if code == 0 else
                           f"--- game data setup stopped (exit code {code}); click DOWNLOAD GAME DATA to resume ---")
        threading.Thread(target=follow, args=(self.setup_proc,), daemon=True).start()

    def read_output(self, proc):
        for raw in proc.stdout:
            self.lines.put(raw.decode("utf-8", "replace").rstrip())
        self.lines.put(f"--- server stopped (exit code {proc.wait()}) ---")

    def stop(self):
        if self.running():
            self.append("--- stopping server ---")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
            self.stopped_by_user = True
        self.refresh_status()

    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def refresh_status(self):
        port = self.int_var(self.var_port, 8080)
        if self.running():
            self.stopped_by_user = False
            # probe in a thread: the UI must not freeze while the server loads its data
            probe = server_alive if self.api_ok else api_answers
            threading.Thread(target=lambda: setattr(self, "api_ok", probe(port)), daemon=True).start()
            text, color = ("ONLINE", GREEN) if self.api_ok else ("STARTING...", ORANGE)
        else:
            self.api_ok = False
            crashed = self.proc is not None and not getattr(self, "stopped_by_user", False)
            text, color = ("STOPPED - see SERVER LOG", RED) if crashed else ("OFFLINE", MUTED)
        self.head.itemconfigure(self.status_text, text=text, fill=color)
        self.head.itemconfigure(self.status_dot, fill=color)
        self.btn_start.set(enabled=not self.running() and not self.busy)
        self.btn_stop.set(enabled=self.running())
        info = (f"Game API: http://127.0.0.1:{port}    BlueStacks: http://10.0.2.2:{port}" if self.running()
                else "Start the server, then launch the game (see CONNECT THE GAME).")
        self.home_info.configure(text=info)
        if getattr(self, "_status_job", None):
            self.root.after_cancel(self._status_job)
        self._status_job = self.root.after(1500, self.refresh_status)

    # ----- logs / UI queue -----

    def append(self, line: str):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        extra = int(self.log.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if extra > 0:
            self.log.delete("1.0", f"{extra + 1}.0")
        self.log.configure(state="disabled")
        self.log.see("end")

    def pump(self):
        try:
            for _ in range(500):
                self.append(self.lines.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                self.ui.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    # ----- tools -----

    def open_admin(self):
        if not self.var_password.get().strip():
            self.show("settings")
            return messagebox.showinfo(TITLE, "Set an admin password first (SETTINGS), then restart the server.")
        webbrowser.open(f"http://127.0.0.1:{self.int_var(self.var_admin_port, 8082)}/saoub-admin/")

    def backups_dir(self) -> str:
        # named like the existing local backups (server/backup_local_*/ is gitignored)
        return os.path.join(os.path.dirname(os.path.abspath(self.var_db.get().strip())), "backup_local_launcher")

    def backup(self, quiet=False) -> str:
        db = self.var_db.get().strip()
        if not os.path.exists(db):
            if not quiet:
                messagebox.showinfo(TITLE, "No database yet (it is created by the first game launch).")
            return ""
        os.makedirs(self.backups_dir(), exist_ok=True)
        out = os.path.join(self.backups_dir(), "saoub_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                           + ".sqlite3")
        try:
            # online backup API: consistent copy even while the server is writing
            src, dst = sqlite3.connect(db), sqlite3.connect(out)
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
        except sqlite3.Error as e:
            messagebox.showerror(TITLE, f"Backup failed:\n{e}")
            return ""
        self.append(f"--- database backed up to {out} ---")
        if not quiet:
            messagebox.showinfo(TITLE, f"Backup saved:\n{out}\n\nTo restore: stop the server, then copy it over\n{db}")
        return out

    def open_logs(self):
        d = os.path.join(SERVER_DIR, "logs")
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    def open_backups(self):
        os.makedirs(self.backups_dir(), exist_ok=True)
        os.startfile(self.backups_dir())

    def install_apk(self):
        apk = self.var_apk.get().strip()
        if not os.path.isfile(apk):
            return messagebox.showerror(TITLE, "Pick the patched .apk file first.")
        if not os.path.exists(BLUESTACKS_ADB):
            return messagebox.showerror(TITLE, "BlueStacks 5 not found. Install it, or drag the .apk onto your "
                                        "emulator's window.")
        self.save_settings()
        self.append(f"--- installing {os.path.basename(apk)} into BlueStacks (this takes a minute) ---")
        self.show("logs")

        adb = self.adb

        def work():
            remote = "/data/local/tmp/saoub_install.apk"
            try:
                dev = self.bluestacks_device()
                if not dev:
                    out = "no BlueStacks device found"
                else:
                    # push + pm install: a streamed `adb install` can hang when BlueStacks is listed twice
                    out = adb("-s", dev, "push", apk, remote, timeout=900).splitlines()[-1]  # not the progress lines
                    out += "\n" + adb("-s", dev, "shell", "pm", "install", "-r", remote, timeout=300)
                    adb("-s", dev, "shell", "rm", "-f", remote)
            except (OSError, subprocess.TimeoutExpired) as e:
                out = f"failed: {e}"
            self.lines.put("adb: " + out.replace("\n", "\n     "))
            if "Success" not in out:
                self.lines.put("Is BlueStacks open, with Android Debug Bridge enabled (Settings > Advanced)? If the "
                               "signature differs from an installed copy, uninstall the game first.")
            else:
                self.lines.put("adb: " + self.push_fps())
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def adb(*args, timeout=60) -> str:
        r = subprocess.run([BLUESTACKS_ADB, *args], capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        return (r.stdout + r.stderr).decode("utf-8", "replace").strip()

    def bluestacks_device(self) -> str:
        """BlueStacks shows up as emulator-5554 and/or 127.0.0.1:5555 (the same instance): use one."""
        for attempt in range(2):
            found = [line.split()[0] for line in self.adb("devices").splitlines()[1:]
                     if line.strip().endswith("\tdevice")]
            if found:
                return sorted(found, key=lambda d: not d.startswith("emulator-"))[0]
            self.adb("connect", BLUESTACKS_DEVICE, timeout=30)
        return ""

    def push_fps(self) -> str:
        """Writes the frame-rate choice where the patched game reads it at start (no file = game default)."""
        fps = FPS_CHOICES.get(self.var_fps.get(), 0)
        try:
            dev = self.bluestacks_device()
            if not dev:
                return "frame rate not sent: BlueStacks is not running (saved, sent at the next SAVE or install)"
            cmd = (f"mkdir -p {os.path.dirname(FPS_FILE)} && echo {fps} > {FPS_FILE}" if fps
                   else f"rm -f {FPS_FILE}")
            out = self.adb("-s", dev, "shell", cmd, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"frame rate not sent: {e}"
        return f"frame rate set to {fps or 60} fps (restart the game)" + (f"\n     {out}" if out else "")

    # ----- updates -----

    def pending_parts(self) -> list:
        """Parts of self.manifest newer than what is installed."""
        m = self.manifest or {}
        parts = []
        if "code" in m and m["code"]["version"] > updater.installed_code_version():
            parts.append("code")
        if "data" in m and m["data"]["version"] > updater.installed_data_version(self.var_data.get().strip()):
            parts.append("data")
        apk = m.get("apk")
        if apk and not os.path.exists(os.path.join(DOWNLOADS, f"saoub_{apk['version']}.apk")):
            parts.append("apk")
        return parts

    def refresh_update_page(self, error=""):
        installed = (f"Installed: launcher/server v{updater.installed_code_version()}, "
                     f"data v{updater.installed_data_version(self.var_data.get().strip())}")
        if updater.developer_checkout():
            self.update_info.configure(text=installed + "\n\nDeveloper checkout (git): updates are off here, use "
                                       "git pull. Publish player updates with tools/launcher/publish_update.py.")
            self.btn_check.set(enabled=False)
            return
        if not updater.config().get("url"):
            self.update_info.configure(text=installed + "\n\nNo update server configured.")
            self.btn_check.set(enabled=False)
            return
        parts = self.pending_parts() if self.manifest else []
        if error:
            status = error
        elif self.manifest is None:
            status = "Click CHECK FOR UPDATES."
        elif parts:
            names = {"code": "launcher/server", "data": "game data", "apk": "game APK"}
            status = f"Update {self.manifest['version']} available: " + ", ".join(names[p] for p in parts) + "."
        else:
            status = "You are up to date."
        self.update_info.configure(text=f"{installed}\n\n{status}")
        self.update_notes.configure(text=(self.manifest or {}).get("notes", "") if parts else "")
        self.btn_update.set(enabled=bool(parts) and not self.busy)
        self.btn_check.set(enabled=not self.busy)
        self.update_badge.configure(text=f"\u25b6  Update {self.manifest['version']} available - click to install"
                                    if parts else "")

    def check_update(self, quiet=False):
        def work():
            try:
                m = updater.check()
                self.ui.put(lambda: (setattr(self, "manifest", m), self.refresh_update_page()))
            except Exception as e:
                msg = str(e)
                self.ui.put(lambda: self.refresh_update_page("" if quiet else msg))
        self.update_info.configure(text="Checking...")
        threading.Thread(target=work, daemon=True).start()

    def run_update(self):
        parts = self.pending_parts()
        if not parts or self.busy:
            return
        if self.running():
            if not messagebox.askyesno(TITLE, "The server must stop during the update. Stop it now?"):
                return
            self.stop()
        self.backup(quiet=True)
        self.busy = True
        self.refresh_update_page()
        data_dir, m = self.var_data.get().strip(), self.manifest
        os.makedirs(DOWNLOADS, exist_ok=True)

        def progress(name):
            def cb(done, total):
                frac = done / total if total else 0
                self.ui.put(lambda: (self.progress.configure(value=frac * 1000),
                                     self.progress_text.configure(text=f"{name}: {done // 1048576} / "
                                                                       f"{max(total, done) // 1048576} MB")))
            return cb

        def work():
            msgs = []
            try:
                if "data" in parts:
                    z = updater.download(m["data"], os.path.join(DOWNLOADS, "data_update.zip"), progress("game data"))
                    n = updater.apply_data(z, data_dir, m["data"]["version"])
                    os.remove(z)
                    msgs.append(f"game data v{m['data']['version']} ({n} files)")
                if "apk" in parts:
                    apk = updater.download(m["apk"], os.path.join(DOWNLOADS, f"saoub_{m['apk']['version']}.apk"),
                                           progress("game APK"))
                    self.ui.put(lambda: (self.var_apk.set(apk), self.save_settings()))
                    msgs.append("game APK (install it from CONNECT THE GAME)")
                restart = False
                if "code" in parts:
                    req = os.path.join(ROOT, "requirements.txt")
                    old_req = open(req, "rb").read() if os.path.exists(req) else b""
                    z = updater.download(m["code"], os.path.join(DOWNLOADS, "code_update.zip"),
                                         progress("launcher/server"))
                    updater.apply_code(z)
                    os.remove(z)
                    # (the standalone exe bundles its libraries: a new one needs a new bundle)
                    if not FROZEN and os.path.exists(req) and open(req, "rb").read() != old_req:
                        self.lines.put("--- installing Python packages ---")
                        r = subprocess.run([server_python(), "-m", "pip", "install", "-r", req], capture_output=True,
                                           creationflags=CREATE_NO_WINDOW)
                        self.lines.put((r.stdout + r.stderr).decode("utf-8", "replace")[-2000:])
                    msgs.append(f"launcher/server v{m['code']['version']}")
                    restart = True
                self.ui.put(lambda: self.update_done("Updated: " + ", ".join(msgs) + ".", restart))
            except Exception as e:
                err = str(e)
                self.ui.put(lambda: self.update_done(f"Update failed: {err}", False, failed=True))
        threading.Thread(target=work, daemon=True).start()

    def update_done(self, message, restart, failed=False):
        self.busy = False
        self.progress.configure(value=0 if failed else 1000)
        self.progress_text.configure(text="")
        self.update_footer()
        self.refresh_update_page(message if failed else "")
        if failed:
            return messagebox.showerror(TITLE, message)
        if restart:
            messagebox.showinfo(TITLE, message + "\n\nThe launcher restarts now.")
            exe, args = sys.executable, [] if FROZEN else [os.path.abspath(__file__)]
            if os.path.basename(exe).lower() == "python.exe" and os.path.exists(exe[:-10] + "pythonw.exe"):
                exe = exe[:-10] + "pythonw.exe"  # no console window
            subprocess.Popen([exe] + args, cwd=ROOT)
            self.root.destroy()
        else:
            messagebox.showinfo(TITLE, message)

    def on_close(self):
        if self.busy and not messagebox.askyesno(TITLE, "An update is running. Quit anyway?"):
            return
        if self.running():
            if not messagebox.askyesno(TITLE, "The server is running. Stop it and quit?"):
                return
            self.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    Launcher(root)
    root.mainloop()


if __name__ == "__main__":
    main()
