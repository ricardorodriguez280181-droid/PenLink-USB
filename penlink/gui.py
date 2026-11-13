#!/usr/bin/env python3
"""PenLink control window: pick your options, press Start, enter the PIN on the iPad once."""
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import auth
import pen

HERE = Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "penlink_settings.json"
PORT = 8765
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

MODES = [
    ("H.264 (GPU, sharp and efficient)", "h264"),
    ("HEVC (GPU, even smaller)", "hevc"),
    ("JPEG (simple, lowest lag)", "jpeg"),
]
DEFAULTS = {
    "mode": "h264", "monitor": 1, "width": "1920", "fps": "60", "bitrate": "12",
    "quality": "70", "software": False, "capture": "ddagrab", "demo": False,
}


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def monitor_choices():
    """[(label, index)] for the monitor drop-down."""
    try:
        import mss
        with mss.mss() as sct:
            out = []
            for i, m in enumerate(sct.monitors):
                if i == 0:
                    label = "0: all monitors combined (needs capture method gdigrab)"
                else:
                    label = "%d: %dx%d%s" % (i, m["width"], m["height"], "  (main)" if i == 1 else "")
                out.append((label, i))
            return out
    except Exception:
        return [("1: main monitor", 1), ("2: second monitor", 2)]


def server_python():
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        exe = exe.with_name("python.exe")  # the server needs working stdout for the log
    return str(exe)


class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.lines = queue.Queue()
        self.last_error = ""
        self.settings = dict(DEFAULTS)
        try:
            self.settings.update(json.loads(SETTINGS_FILE.read_text()))
        except (OSError, ValueError):
            pass

        root.title("PenLink")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.minsize(560, 0)
        frame = ttk.Frame(root, padding=14)
        frame.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(8, weight=1)

        self.monitors = monitor_choices()
        self.v_monitor = tk.StringVar()
        self.v_mode = tk.StringVar()
        self.v_width = tk.StringVar(value=str(self.settings["width"]))
        self.v_fps = tk.StringVar(value=str(self.settings["fps"]))
        self.v_bitrate = tk.StringVar(value=str(self.settings["bitrate"]))
        self.v_quality = tk.StringVar(value=str(self.settings["quality"]))
        self.v_capture = tk.StringVar(value=self.settings["capture"])
        self.v_software = tk.BooleanVar(value=bool(self.settings["software"]))
        self.v_demo = tk.BooleanVar(value=bool(self.settings["demo"]))

        labels = [m[0] for m in self.monitors]
        wanted = [m[0] for m in self.monitors if m[1] == self.settings["monitor"]]
        self.v_monitor.set(wanted[0] if wanted else labels[min(1, len(labels) - 1)])
        self.v_mode.set(next((l for l, v in MODES if v == self.settings["mode"]), MODES[0][0]))

        self.controls = []
        def row(r, text, widget):
            ttk.Label(frame, text=text).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 12))
            widget.grid(row=r, column=1, sticky="ew", pady=3)
            self.controls.append(widget)
            return widget

        row(0, "Monitor", ttk.Combobox(frame, textvariable=self.v_monitor, values=labels, state="readonly"))
        mode_box = row(1, "Video mode", ttk.Combobox(frame, textvariable=self.v_mode,
                                                     values=[m[0] for m in MODES], state="readonly"))
        mode_box.bind("<<ComboboxSelected>>", lambda e: self.refresh_enabled())
        row(2, "Max width (pixels)", ttk.Combobox(frame, textvariable=self.v_width,
                                                  values=["1280", "1600", "1920", "2560"]))
        row(3, "Frames per second", ttk.Combobox(frame, textvariable=self.v_fps, values=["30", "45", "60"]))
        self.w_bitrate = row(4, "Bitrate (Mbps, H.264/HEVC)", ttk.Spinbox(frame, from_=2, to=60, textvariable=self.v_bitrate))
        self.w_quality = row(5, "Quality (1-95, JPEG)", ttk.Spinbox(frame, from_=20, to=95, textvariable=self.v_quality))

        opts = ttk.Frame(frame)
        opts.grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 8))
        self.w_software = ttk.Checkbutton(opts, text="Encode on the CPU instead of the GPU", variable=self.v_software)
        self.w_software.grid(row=0, column=0, sticky="w", padx=(0, 16))
        self.w_demo = ttk.Checkbutton(opts, text="Test mode (demo canvas, no pen input)", variable=self.v_demo)
        self.w_demo.grid(row=0, column=1, sticky="w")
        self.controls += [self.w_software, self.w_demo]

        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.btn = ttk.Button(buttons, text="Start", command=self.toggle, width=14)
        self.btn.grid(row=0, column=0, padx=(0, 10))
        self.status = ttk.Label(buttons, text="Stopped")
        self.status.grid(row=0, column=1, sticky="w")
        ttk.Button(buttons, text="Forget paired iPads", command=self.forget).grid(row=0, column=2, padx=(12, 0))
        buttons.columnconfigure(1, weight=1)

        info = ttk.LabelFrame(frame, text="Connect the iPad", padding=10)
        info.grid(row=8, column=0, columnspan=2, sticky="nsew")
        info.columnconfigure(1, weight=1)
        ttk.Label(info, text="1. In Safari on the iPad, open:").grid(row=0, column=0, sticky="w")
        self.v_url = tk.StringVar(value="(press Start)")
        ttk.Entry(info, textvariable=self.v_url, state="readonly", font=("Segoe UI", 11)).grid(
            row=0, column=1, sticky="ew", padx=8)
        ttk.Button(info, text="Copy", command=self.copy_url, width=7).grid(row=0, column=2)
        ttk.Label(info, text="2. First time only, enter this PIN:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.v_pin = tk.StringVar(value="------")
        ttk.Label(info, textvariable=self.v_pin, font=("Segoe UI", 22, "bold")).grid(
            row=1, column=1, sticky="w", padx=8, pady=(8, 0))

        self.log = tk.Text(frame, height=9, state="disabled", wrap="word", font=("Consolas", 9))
        self.log.grid(row=9, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        frame.rowconfigure(9, weight=1)

        self.refresh_enabled()
        self.poll()

    # ---- helpers ------------------------------------------------------------------
    def mode_value(self):
        return next((v for l, v in MODES if l == self.v_mode.get()), "h264")

    def monitor_value(self):
        return next((i for l, i in self.monitors if l == self.v_monitor.get()), 1)

    def refresh_enabled(self):
        jpeg = self.mode_value() == "jpeg"
        self.w_bitrate.state(["disabled"] if jpeg else ["!disabled"])
        self.w_software.state(["disabled"] if jpeg else ["!disabled"])
        self.w_quality.state(["!disabled"] if jpeg else ["disabled"])

    def set_controls(self, enabled):
        for w in self.controls:
            try:
                w.state(["!disabled"] if enabled else ["disabled"])
            except tk.TclError:
                pass
        if enabled:
            self.refresh_enabled()

    def add_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 400:
            self.log.delete("1.0", "50.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def copy_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.v_url.get())

    # ---- start / stop ---------------------------------------------------------------
    def collect(self):
        return {
            "mode": self.mode_value(), "monitor": self.monitor_value(),
            "width": self.v_width.get().strip() or "1920", "fps": self.v_fps.get().strip() or "60",
            "bitrate": self.v_bitrate.get().strip() or "12", "quality": self.v_quality.get().strip() or "70",
            "software": self.v_software.get(), "capture": self.v_capture.get(), "demo": self.v_demo.get(),
        }

    def toggle(self):
        if self.proc is None:
            self.start()
        else:
            self.stop()

    def start(self):
        s = self.collect()
        for key in ("width", "fps", "bitrate", "quality"):
            if not s[key].isdigit():
                messagebox.showerror("PenLink", "%s must be a whole number." % key)
                return
        try:
            SETTINGS_FILE.write_text(json.dumps(s, indent=2))
        except OSError:
            pass
        pin = auth.new_pin()
        cmd = [server_python(), "-u", str(HERE / "server.py"), "--port", str(PORT),
               "--codec", s["mode"], "--monitor", str(s["monitor"]), "--max-width", s["width"],
               "--fps", s["fps"], "--bitrate", s["bitrate"] + "M", "--quality", s["quality"],
               "--capture", "gdigrab" if s["monitor"] == 0 else s["capture"], "--pin", pin]
        if s["software"]:
            cmd.append("--software")
        if s["demo"]:
            cmd.append("--demo")
        self.last_error = ""
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                cwd=str(HERE), creationflags=NO_WINDOW, text=True, encoding="utf-8",
                errors="replace", bufsize=1)
        except OSError as exc:
            messagebox.showerror("PenLink", "Could not start the server:\n%s" % exc)
            return
        threading.Thread(target=self.read_output, args=(self.proc,), daemon=True).start()
        self.v_url.set("http://%s:%d" % (lan_ip(), PORT))
        self.v_pin.set(pin)
        self.btn.configure(text="Stop")
        self.status.configure(text="Starting…")
        self.set_controls(False)
        self.add_log("--- started (%s) ---" % s["mode"])

    def read_output(self, proc):
        for line in proc.stdout:
            self.lines.put((proc, line.rstrip()))
        self.lines.put((proc, None))

    def stop(self):
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            if os.name == "nt":
                # /T also ends the ffmpeg process the server started
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, creationflags=NO_WINDOW)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        self.btn.configure(text="Start")
        self.status.configure(text="Stopped" + (" - " + self.last_error if self.last_error else ""))
        self.v_url.set("(press Start)")
        self.v_pin.set("------")
        self.set_controls(True)

    def poll(self):
        try:
            while True:
                proc, line = self.lines.get_nowait()
                if proc is not self.proc:
                    continue  # output from a server that was already stopped
                if line is None:
                    continue
                self.add_log(line)
                low = line.lower()
                if line.startswith("error:"):
                    self.last_error = line[6:].strip()
                if "iPad connected" in line:
                    self.status.configure(text="iPad connected")
                elif "iPad disconnected" in line or "PenLink is running" in line:
                    self.status.configure(text="Running, waiting for the iPad")
                elif "paired" in low and "wrong" not in low:
                    self.status.configure(text="iPad paired")
        except queue.Empty:
            pass
        if self.proc is not None and self.proc.poll() is not None:
            self.add_log("--- server stopped (exit code %s) ---" % self.proc.returncode)
            self.stop()
        elif self.proc is not None and self.status.cget("text") == "Starting…":
            self.status.configure(text="Starting…")
        self.root.after(200, self.poll)

    def forget(self):
        if not messagebox.askyesno("PenLink", "Forget every paired iPad? They will need the PIN again."):
            return
        if self.proc is not None:
            self.stop()
        try:
            auth.SECRET_FILE.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            messagebox.showerror("PenLink", "Could not delete %s:\n%s" % (auth.SECRET_FILE, exc))
            return
        messagebox.showinfo("PenLink", "Done. Press Start again to pair an iPad with the new PIN.")

    def on_close(self):
        self.stop()
        self.root.destroy()


def main():
    pen.enable_dpi_awareness()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
