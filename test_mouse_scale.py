"""Scale relative mouse movement by equipped weapon while aiming in MTA."""

import ctypes as ct
import json
import math
import queue
import subprocess
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import messagebox, ttk

MULTIPLIERS = {
    28: 1.44,  # Micro Uzi
    30: 1.00,  # AK-47
    32: 6.00,  # Tec-9
    34: 0.10,  # Sniper Rifle
}

WEAPONS = (
    (22, "Colt 45", "Colt45.png"), (24, "Desert Eagle", "Deagle.png"),
    (25, "Shotgun", "Shotgun.png"), (26, "Sawed-off", "Sawnoff.png"),
    (27, "SPAS-12", "Spas12.png"), (28, "Micro Uzi", "Mac10.png"),
    (29, "MP5", "Mp5.png"), (32, "Tec-9", "Tec9.png"),
    (30, "AK-47", "Ak47.png"), (31, "M4", "M4.png"),
    (33, "Rifle", "Rifle.png"), (34, "Sniper", "Sniper.png"),
    (38, "Minigun", "Minigun.png"),
)
WEAPON_NAMES = {weapon: name for weapon, name, _ in WEAPONS}
ASSET_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = ASSET_DIR / "sensitivity.json"

WEAPON_POLL_SECONDS = 0.01
PROCESS_VM_READ = 0x0010
PLAYER_PED_PTR = 0xB7CD98
WEAPONS_OFFSET = 0x5A0
SELECTED_SLOT_OFFSET = 0x718
WEAPON_SIZE = 0x1C

kernel32 = ct.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.ReadProcessMemory.argtypes = (
    wintypes.HANDLE, ct.c_void_p, ct.c_void_p, ct.c_size_t,
    ct.POINTER(ct.c_size_t),
)
kernel32.ReadProcessMemory.restype = wintypes.BOOL


def find_game_pid():
    output = subprocess.check_output(
        ["powershell.exe", "-NoProfile", "-Command",
         "Get-Process -Name gta_sa,gta-sa -ErrorAction SilentlyContinue "
         "| Select-Object -ExpandProperty Id; exit 0"],
        text=True,
    ).split()
    if len(output) != 1:
        raise RuntimeError("Start exactly one GTA San Andreas or MTA client.")
    return int(output[0])


def read_memory(handle, address, size):
    buffer = ct.create_string_buffer(size)
    count = ct.c_size_t()
    if not kernel32.ReadProcessMemory(handle, address, buffer, size, ct.byref(count)):
        raise ct.WinError(ct.get_last_error())
    if count.value != size:
        raise OSError(f"Incomplete read at 0x{address:08X}.")
    return buffer.raw


def equipped_weapon_id(handle):
    ped = int.from_bytes(read_memory(handle, PLAYER_PED_PTR, 4), "little")
    if not ped:
        return None
    data = read_memory(
        handle, ped + WEAPONS_OFFSET, SELECTED_SLOT_OFFSET - WEAPONS_OFFSET + 1,
    )
    slot = data[-1]
    if slot >= 13:
        raise RuntimeError(f"Invalid weapon slot {slot}. Check the GTA SA build.")
    start = slot * WEAPON_SIZE
    return int.from_bytes(data[start:start + 4], "little")


def scale_axis(count, multiplier, remainder):
    scaled = count * multiplier + remainder
    whole = math.trunc(scaled)
    if not -2147483648 <= whole <= 2147483647:
        return max(-2147483648, min(2147483647, whole)), 0.0
    return whole, scaled - whole


class MouseStroke(ct.Structure):
    _fields_ = [
        ("state", ct.c_ushort), ("flags", ct.c_ushort),
        ("rolling", ct.c_short), ("x", ct.c_int), ("y", ct.c_int),
        ("information", ct.c_uint),
    ]


def run_filter(get_multipliers, stop, report):
    dll = ct.CDLL(str(Path(__file__).with_name("interception.dll").resolve()))
    predicate = ct.CFUNCTYPE(ct.c_int, ct.c_int)
    dll.interception_create_context.argtypes = ()
    dll.interception_create_context.restype = ct.c_void_p
    dll.interception_destroy_context.argtypes = (ct.c_void_p,)
    dll.interception_destroy_context.restype = None
    dll.interception_is_mouse.argtypes = (ct.c_int,)
    dll.interception_is_mouse.restype = ct.c_int
    mouse_only = predicate(dll.interception_is_mouse)
    dll.interception_set_filter.argtypes = (ct.c_void_p, predicate, ct.c_ushort)
    dll.interception_set_filter.restype = None
    dll.interception_wait_with_timeout.argtypes = (ct.c_void_p, ct.c_ulong)
    dll.interception_wait_with_timeout.restype = ct.c_int
    for function in (dll.interception_receive, dll.interception_send):
        function.argtypes = (ct.c_void_p, ct.c_int, ct.POINTER(MouseStroke), ct.c_uint)
        function.restype = ct.c_int

    user32 = ct.WinDLL("user32")
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND, ct.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetAsyncKeyState.argtypes = (ct.c_int,)
    user32.GetAsyncKeyState.restype = ct.c_short

    pid = find_game_pid()
    handle = kernel32.OpenProcess(PROCESS_VM_READ, False, pid)
    if not handle:
        raise ct.WinError(ct.get_last_error())
    context = dll.interception_create_context()
    if not context:
        kernel32.CloseHandle(handle)
        raise RuntimeError("Interception driver is unavailable. Install it and reboot.")

    held = set()
    stroke = MouseStroke()
    foreground_pid = wintypes.DWORD()
    remainders = {}
    previous_sample = None
    confirmed_weapon = None
    next_poll = 0.0
    previous_window = None
    try:
        report("Active · Hold right-click in MTA · F10 stops")
        active_multipliers = get_multipliers()
        dll.interception_set_filter(context, mouse_only, 0x100C)
        while not stop.is_set() and not (user32.GetAsyncKeyState(0x79) & 0x8000):
            latest = get_multipliers()
            if latest is not active_multipliers:
                active_multipliers = latest
                remainders.clear()
            window = user32.GetForegroundWindow()
            if window != previous_window:
                foreground_pid.value = 0
                if window:
                    user32.GetWindowThreadProcessId(window, ct.byref(foreground_pid))
                previous_window = window
                remainders.clear()

            now = time.monotonic()
            if foreground_pid.value == pid and now >= next_poll:
                weapon = equipped_weapon_id(handle)
                if weapon == previous_sample and weapon != confirmed_weapon:
                    confirmed_weapon = weapon
                    remainders.clear()
                    report(f"Active · {WEAPON_NAMES.get(weapon, 'Unconfigured weapon')} · F10 stops")
                previous_sample = weapon
                next_poll = now + WEAPON_POLL_SECONDS

            device = dll.interception_wait_with_timeout(context, 10)
            if not device:
                continue
            if dll.interception_receive(context, device, ct.byref(stroke), 1) != 1:
                raise RuntimeError("Could not read a mouse packet.")
            if stroke.state & 0x0004:
                held.add(device)
            if stroke.state & 0x0008:
                held.discard(device)
                remainders.pop(device, None)
            multiplier = active_multipliers.get(confirmed_weapon, 1.0)
            if (
                multiplier != 1.0 and device in held and not (stroke.flags & 1)
                and foreground_pid.value == pid
                and user32.GetForegroundWindow() == window
            ):
                x_left, y_left = remainders.get(device, (0.0, 0.0))
                stroke.x, x_left = scale_axis(stroke.x, multiplier, x_left)
                stroke.y, y_left = scale_axis(stroke.y, multiplier, y_left)
                remainders[device] = (x_left, y_left)
            if dll.interception_send(context, device, ct.byref(stroke), 1) != 1:
                raise RuntimeError("Could not forward a mouse packet.")
    finally:
        try:
            dll.interception_set_filter(context, mouse_only, 0)
        finally:
            dll.interception_destroy_context(context)
            kernel32.CloseHandle(handle)


class SensitivityWindow:
    def __init__(self, root):
        self.root = root
        self.stop = threading.Event()
        self.updates = queue.SimpleQueue()
        self.worker = None
        self.closing = False
        self.sliders = {}
        self.images = []
        self.applied = {weapon: MULTIPLIERS.get(weapon, 1.0) for weapon in WEAPON_NAMES}
        self.status = tk.StringVar(value="Ready · Start MTA, then enable the filter")
        try:
            if SETTINGS_PATH.exists():
                saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                self.applied.update({
                    weapon: float(saved[str(weapon)])
                    for weapon in self.applied
                    if str(weapon) in saved and 0.1 <= float(saved[str(weapon)]) <= 6
                })
        except (OSError, ValueError, TypeError):
            self.status.set("Could not read saved settings · Using defaults")

        root.title("MTA Sensitivity")
        root.configure(bg="#101620", padx=24, pady=18)
        root.resizable(False, False)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("Aim.Horizontal.TScale", background="#1a2331",
                        troughcolor="#0e1520", sliderlength=16)
        self.label(root, "MTA Sensitivity", 23, "#f4f7fb", "#101620", bold=True).pack(anchor="w")
        self.label(root, "Per-weapon aim multipliers · Right-click to aim", 10,
                   "#a9b7ca", "#101620").pack(anchor="w", pady=(2, 13))

        grid = tk.Frame(root, bg="#101620")
        grid.pack(fill="x")
        for index, (weapon, name, filename) in enumerate(WEAPONS):
            self.weapon_row(grid, index, weapon, name, filename)

        controls = tk.Frame(root, bg="#101620")
        controls.pack(fill="x", pady=(14, 8))
        self.button(controls, "Apply", self.apply, "#94e3ba", "#10251a").pack(side="left")
        self.button(controls, "Reset all to 1×", self.reset, "#263447", "#eef4fa").pack(side="left", padx=8)
        self.toggle_button = self.button(controls, "Enable filter", self.toggle, "#263447", "#eef4fa")
        self.toggle_button.pack(side="right")
        tk.Label(root, textvariable=self.status, bg="#101620", fg="#a9b7ca",
                 font=("Segoe UI", 10), wraplength=650, justify="left").pack(anchor="w")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(50, self.poll)

    @staticmethod
    def label(parent, text, size, fg, bg, bold=False):
        return tk.Label(parent, text=text, fg=fg, bg=bg,
                        font=("Segoe UI", size, "bold" if bold else "normal"))

    @staticmethod
    def button(parent, text, command, bg, fg):
        return tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                         activebackground=bg, activeforeground=fg, relief="flat",
                         padx=13, pady=6, cursor="hand2", font=("Segoe UI", 10, "bold"))

    def weapon_row(self, grid, index, weapon, name, filename):
        bg = "#1a2331"
        card = tk.Frame(grid, bg=bg, padx=10, pady=6)
        card.grid(row=index // 2, column=index % 2, padx=(0, 8), pady=(0, 6), sticky="ew")
        icon = tk.PhotoImage(file=ASSET_DIR / filename)
        self.images.append(icon)
        image_box = tk.Frame(card, bg=bg, width=90, height=24)
        image_box.pack_propagate(False)
        image_box.pack(side="left")
        tk.Label(image_box, image=icon, bg=bg).pack(expand=True)
        body = tk.Frame(card, bg=bg)
        body.pack(side="left", fill="x", expand=True, padx=(8, 0))
        heading = tk.Frame(body, bg=bg)
        heading.pack(fill="x")
        self.label(heading, name, 10, "#f4f7fb", bg, bold=True).pack(side="left")
        value = tk.StringVar(value=f"{self.applied[weapon]:.2f}×")
        tk.Label(heading, textvariable=value, fg="#94e3ba", bg=bg,
                 font=("Segoe UI", 10, "bold")).pack(side="right")
        slider = ttk.Scale(body, from_=0.1, to=6.0, length=206,
                           style="Aim.Horizontal.TScale",
                           command=lambda number: value.set(f"{float(number):.2f}×"))
        slider.set(self.applied[weapon])
        slider.pack(fill="x")
        self.sliders[weapon] = slider

    def apply(self):
        values = {weapon: round(slider.get(), 2) for weapon, slider in self.sliders.items()}
        try:
            temp = SETTINGS_PATH.with_suffix(".tmp")
            temp.write_text(json.dumps(values, indent=2), encoding="utf-8")
            temp.replace(SETTINGS_PATH)
        except OSError as error:
            messagebox.showerror("Could not save settings", str(error), parent=self.root)
            return
        self.applied = values
        active = self.worker is not None and self.worker.is_alive()
        self.status.set("Settings applied · Filter active" if active else "Settings saved · Filter is off")

    def reset(self):
        for slider in self.sliders.values():
            slider.set(1.0)
        self.apply()

    def toggle(self):
        if self.worker is not None:
            self.stop.set()
            return
        self.stop.clear()
        self.status.set("Connecting to MTA…")
        self.toggle_button.configure(text="Stop filter")
        self.worker = threading.Thread(target=self.run)
        self.worker.start()

    def run(self):
        try:
            run_filter(lambda: self.applied, self.stop, self.updates.put)
        except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
            self.updates.put(str(error))
        else:
            self.updates.put("Filter stopped")

    def poll(self):
        while not self.updates.empty():
            self.status.set(self.updates.get_nowait())
        if self.worker is not None and not self.worker.is_alive():
            self.worker = None
            self.toggle_button.configure(text="Enable filter")
        if self.closing and self.worker is None:
            self.root.destroy()
        else:
            self.root.after(50, self.poll)

    def close(self):
        self.closing = True
        self.stop.set()


if __name__ == "__main__":
    window = tk.Tk()
    SensitivityWindow(window)
    window.mainloop()
