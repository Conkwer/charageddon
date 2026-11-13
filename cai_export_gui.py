#!/usr/bin/env python3
"""Tkinter front-end for cai_export.py.

Build (console + GUI, so progress is visible in both):

    pyinstaller --noconfirm --onefile --console --name cai-backup-gui cai_export_gui.py

The token is kept in memory only and never written to disk. The output folder
and the "full backup" flag are remembered in settings.ini next to the
executable.
"""
import configparser
import queue
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import cai_export
except ModuleNotFoundError:
    # A onefile build normally bundles it. This fallback lets the exe work
    # when cai_export.py is shipped next to it instead.
    _here = (Path(sys.executable).resolve().parent
             if getattr(sys, "frozen", False) else Path(__file__).resolve().parent)
    sys.path.insert(0, str(_here))
    try:
        import cai_export
    except ModuleNotFoundError:
        _msg = (
            "cai_export.py was not included in this build and is not next to "
            "the executable.\n\n"
            "Rebuild with both files in the same folder:\n"
            "pyinstaller --noconfirm --onefile --console "
            "--name cai-backup-gui --hidden-import cai_export "
            "cai_export_gui.py")
        try:
            _root = tk.Tk()
            _root.withdraw()
            messagebox.showerror("Characters Backup", _msg)
            _root.destroy()
        except Exception:
            print(_msg, file=sys.stderr)
        raise SystemExit(1)

PAUSE_EVERY = 30


class BackupCancelled(Exception):
    pass


def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


class BackupApp:
    def __init__(self):
        self.app_dir = app_dir()
        self.config_path = self.app_dir / "settings.ini"
        self.log_q = queue.Queue()
        self.ask_q = queue.Queue()
        self.ask_event = threading.Event()
        self.answer = False
        self.running = False
        self.stop_requested = False
        self._spin = 0

        self._enable_hidpi()
        self.root = tk.Tk()
        self.root.title("Characters Backup")
        self.root.geometry("700x620")
        self.root.minsize(620, 520)
        try:
            ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
        except tk.TclError:
            pass

        self._build()
        self._load_settings()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        cai_export.LOG_FN = self.log_q.put
        self.root.after(100, self._poll)
        self._tick_spinner()

    @staticmethod
    def _enable_hidpi():
        if sys.platform != "win32":
            return
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                import ctypes
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    # ------------------------------------------------------------- UI
    def _build(self):
        frm = ttk.Frame(self.root, padding=10)
        frm.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Token").grid(row=0, column=0, sticky="w", pady=3)
        self.token_var = tk.StringVar()
        self.token_entry = ttk.Entry(frm, textvariable=self.token_var, show="*")
        self.token_entry.grid(row=0, column=1, columnspan=2, sticky="ew",
                              padx=6, pady=3)
        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Show", variable=self.show_var,
                        command=self._toggle_token).grid(row=0, column=3,
                                                         sticky="w")

        ttk.Label(
            frm,
            text=("F12 in Chrome → Network → click any item (not a folder) → "
                  "Headers → set-cookie or authorization → copy the value "
                  "after 'Token '."),
            foreground="#555", wraplength=650,
        ).grid(row=1, column=0, columnspan=4, sticky="w", padx=6,
               pady=(0, 6))

        ttk.Label(frm, text="Output folder").grid(row=2, column=0, sticky="w",
                                                  pady=3)
        self.out_var = tk.StringVar(value=str(self.app_dir / "export"))
        ttk.Entry(frm, textvariable=self.out_var).grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=6, pady=3)
        ttk.Button(frm, text="Browse...", command=self._browse).grid(
            row=2, column=3, sticky="ew")

        self.full_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Full backup (every character, not only liked)",
                        variable=self.full_var).grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(8, 2))

        ttk.Label(
            frm,
            text=("Your own account only. Automated access may violate "
                  "Character.AI's terms and can get your account limited or "
                  "banned. No warranty."),
            wraplength=640,
        ).grid(row=4, column=0, columnspan=4, sticky="w", pady=(2, 8))

        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(0, 6))
        self.start_btn = ttk.Button(btns, text="Start backup",
                                    command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Stop", command=self._stop,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.spinner = ttk.Label(btns, text="", width=3)
        self.spinner.pack(side="left", padx=(12, 2))
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(btns, textvariable=self.status_var).pack(side="left")

        font = ("Consolas", 9) if sys.platform == "win32" else ("Courier", 9)
        self.log = tk.Text(frm, height=20, wrap="word", state="disabled",
                           font=font)
        self.log.grid(row=6, column=0, columnspan=4, sticky="nsew",
                      pady=(4, 0))
        sb = ttk.Scrollbar(frm, orient="vertical", command=self.log.yview)
        sb.grid(row=6, column=4, sticky="ns")
        self.log.configure(yscrollcommand=sb.set)
        frm.rowconfigure(6, weight=1)

    def _toggle_token(self):
        self.token_entry.config(show="" if self.show_var.get() else "*")

    def _browse(self):
        chosen = filedialog.askdirectory(initialdir=self.out_var.get() or ".")
        if chosen:
            self.out_var.set(chosen)

    def _append(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", str(msg) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _tick_spinner(self):
        if self.running:
            self._spin = (self._spin + 1) % 4
            self.spinner.configure(text="|/-\\"[self._spin])
        else:
            self.spinner.configure(text="")
        self.root.after(120, self._tick_spinner)

    # --------------------------------------------------------- settings
    def _load_settings(self):
        if not self.config_path.exists():
            return
        cfg = configparser.ConfigParser()
        try:
            cfg.read(self.config_path)
            self.out_var.set(cfg.get("gui", "output",
                                     fallback=self.out_var.get()))
            self.full_var.set(cfg.getboolean("gui", "full", fallback=False))
        except Exception:
            pass

    def _save_settings(self):
        # The token is never saved.
        cfg = configparser.ConfigParser()
        cfg["gui"] = {"output": self.out_var.get().strip(),
                      "full": "1" if self.full_var.get() else "0"}
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                cfg.write(f)
        except OSError:
            pass

    # ----------------------------------------------------------- start
    def _start(self):
        if self.running:
            return
        token = self.token_var.get().strip()
        if token.lower().startswith("token "):
            token = token[6:].strip()
        if not token:
            messagebox.showerror("Token missing",
                                 "Paste your Character.AI token.")
            return
        out = self.out_var.get().strip()
        if not out:
            messagebox.showerror("Output folder missing",
                                 "Choose an output folder.")
            return
        full = bool(self.full_var.get())
        if not messagebox.askokcancel(
                "Before you start",
                "Use this only for your own account.\n\n"
                "Automated access may violate Character.AI's terms of "
                "service. Your account can be limited or banned. There is "
                "no warranty.\n\n"
                "The token is kept in memory only and is never saved.\n"
                "Every 30 characters the backup pauses and asks whether to "
                "continue.\n\n"
                "Start the backup?"):
            return
        try:
            Path(out).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Output folder", str(e))
            return

        self._save_settings()
        self.running = True
        self.stop_requested = False
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_var.set("Working...")
        self._append(f"Output: {out}")
        self._append("Mode: " + ("full account" if full
                                 else "liked only (max 10)"))
        self.worker = threading.Thread(target=self._run,
                                       args=(token, out, full), daemon=True)
        self.worker.start()

    def _stop(self):
        if self.running:
            self.stop_requested = True
            self.status_var.set("Stopping after the current character...")

    # ------------------------------------------------------------ worker
    def _run(self, token, out, full):
        try:
            api = cai_export.Neo(token, rate=0.4, pause_every=0,
                                 pause_seconds=0,
                                 on_character=self._on_character)
            me = api.me()
            account = {
                "username": me.get("username"),
                "name": (me.get("name") or me.get("first_name")
                         or (me.get("account") or {}).get("name")),
                "id": me.get("id"),
                "token": token,
                "avatar_file_name": (me.get("account") or {}).get(
                    "avatar_file_name"),
            }
            cai_export.USER_AVATAR = ""
            if account.get("avatar_file_name"):
                try:
                    raw, _ = api.image(account["avatar_file_name"])
                    cai_export.USER_AVATAR = cai_export.downscale_png(raw)
                except Exception:
                    pass
            cai_export.export_account(api, account, out, full=full)
            self.log_q.put("")
            self.log_q.put("Done.")
        except BackupCancelled:
            self.log_q.put("Stopped by user. Partial files remain in the "
                           "output folder.")
        except Exception as e:
            self.log_q.put(f"ERROR: {e}")
        finally:
            self.log_q.put("__DONE__")

    def _on_character(self, n):
        if self.stop_requested:
            raise BackupCancelled()
        if n % 10 == 0:
            self.log_q.put(f"[{n} characters done]")
        if n % PAUSE_EVERY == 0:
            self.ask_q.put(n)
            self.ask_event.wait()
            self.ask_event.clear()
            if not self.answer or self.stop_requested:
                raise BackupCancelled()

    # -------------------------------------------------------------- poll
    def _poll(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                if msg == "__DONE__":
                    self.running = False
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.status_var.set("Finished")
                else:
                    self._append(msg)
        except queue.Empty:
            pass

        try:
            while True:
                n = self.ask_q.get_nowait()
                self.status_var.set(f"Paused after {n} characters")
                self.answer = messagebox.askyesno(
                    "Continue?",
                    f"{n} characters are backupped, do you want to continue?")
                self.ask_event.set()
                self.status_var.set("Working...")
        except queue.Empty:
            pass

        self.root.after(100, self._poll)

    # ------------------------------------------------------------- close
    def _on_close(self):
        if self.running:
            if not messagebox.askyesno(
                    "Quit",
                    "A backup is running. Stop it and quit?\n"
                    "Partial files will remain in the output folder."):
                return
            self.stop_requested = True
        self.root.destroy()


def main():
    BackupApp().root.mainloop()


if __name__ == "__main__":
    main()
