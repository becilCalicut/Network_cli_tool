import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading
import os
import time
import socket
import traceback
import paramiko
import queue
from datetime import datetime
from pathlib import Path


# ─────────────────────────────────────────────
#  OEM definitions
# ─────────────────────────────────────────────
OEM_LIST = ["Cisco", "Aruba", "Ruckus", "Alcatel"]

OEM_COLORS = {
    "Cisco":   "#049fd9",
    "Aruba":   "#ff6600",
    "Ruckus":  "#c00",
    "Alcatel": "#00adef",
}

# Default sample commands written to each notepad template on first run
OEM_DEFAULT_COMMANDS = {
    "cisco":   "show version\nshow running-config\nshow interfaces status\nshow ip route\n",
    "aruba":   "show version\nshow running-config\nshow interfaces\nshow ip route\n",
    "ruckus":  "show version\nshow running-config\nshow interfaces\nshow ip route\n",
    "alcatel": "show system\nshow running-config\nshow interfaces\nshow ip route\n",
}


# ─────────────────────────────────────────────
#  Output cleaning helpers
# ─────────────────────────────────────────────
import re

# Matches all ANSI/VT100 escape sequences  e.g. \x1b[50;1H  \x1b[2K  \x1b[?25h
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b[=>]|\x1b\([A-Z]')

def _clean(raw: str) -> str:
    """Strip ANSI codes, normalise line endings, remove blank duplicates."""
    text = _ANSI_RE.sub("", raw)          # remove escape sequences
    text = text.replace("\r\r\n", "\n")   # double CR+LF → LF
    text = text.replace("\r\n", "\n")     # CR+LF → LF
    text = text.replace("\r", "\n")       # stray CR → LF
    # Drop lines that are only the prompt echo of the command we just sent
    # (switches often echo back what we typed with cursor-position codes)
    lines = text.splitlines()
    cleaned = []
    for line in lines:
        s = line.strip()
        # Skip pure empty lines run > 2 in a row (keep up to 1 blank separator)
        if not s and cleaned and not cleaned[-1].strip():
            continue
        cleaned.append(line.rstrip())
    return "\n".join(cleaned).strip()


# ─────────────────────────────────────────────
#  SSH helper – connect & run commands
# ─────────────────────────────────────────────
def ssh_run_commands(ip: str, username: str, password: str,
                     commands: list[str], timeout: int = 30,
                     debug: bool = False) -> tuple[bool, str, list[str]]:
    """
    Connect to *ip* via SSH, run each command.
    Returns (success, output, debug_lines).
    debug_lines is always populated; user can decides whether to show it.
    """
    output_lines = []
    debug_lines  = []

    def dbg(msg):
        debug_lines.append(f"    [DBG] {msg}")

    ip = ip.strip()
    dbg(f"Target IP   : {ip}")
    dbg(f"Username    : {username}")
    dbg(f"Timeout     : {timeout}s")
    dbg(f"Commands    : {len(commands)}")

    # ── Step 1: basic TCP reachability
    dbg("Step 1 – TCP port 22 reachability …")
    try:
        sock = socket.create_connection((ip, 22), timeout=timeout)
        sock.close()
        dbg("           ✔ TCP port 22 is open")
    except socket.timeout:
        dbg("           ✗ TIMEOUT – host unreachable or port 22 blocked")
        return False, "Connection timed out (port 22 unreachable)", debug_lines
    except ConnectionRefusedError:
        dbg("           ✗ CONNECTION REFUSED – SSH not running on port 22")
        return False, "Connection refused (SSH not running on port 22)", debug_lines
    except OSError as e:
        dbg(f"           ✗ NETWORK ERROR – {e}")
        return False, f"Network error: {e}", debug_lines

    # ── Step 2: SSH handshake & auth
    dbg("Step 2 – SSH handshake & authentication …")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ip, username=username, password=password,
                       timeout=timeout, look_for_keys=False, allow_agent=False)
        dbg("           ✔ SSH authentication successful")
    except paramiko.AuthenticationException:
        dbg("           ✗ AUTH FAILED – wrong username or password")
        return False, "Authentication failed – check username/password", debug_lines
    except paramiko.SSHException as e:
        dbg(f"           ✗ SSH ERROR – {e}")
        return False, f"SSH protocol error: {e}", debug_lines
    except Exception as e:
        dbg(f"           ✗ UNEXPECTED – {e}")
        dbg(traceback.format_exc())
        return False, f"Unexpected error during connect: {e}", debug_lines

    # ── Step 3: open interactive shell
    # Use a DUMB terminal (term="dumb") so the switch does NOT send
    # cursor-positioning ANSI codes. Width=32000 discourages line-wrapping.
    dbg("Step 3 – Opening interactive shell (dumb terminal, wide width) …")
    try:
        shell = client.invoke_shell(term="dumb", width=32000, height=1000)
        shell.settimeout(timeout)
        dbg("           ✔ Interactive shell opened")
    except Exception as e:
        dbg(f"           ✗ Could not open shell – {e}")
        client.close()
        return False, f"Could not open interactive shell: {e}", debug_lines

    def _read_all(shell, settle=1.2, max_wait=30):
        """
        Read output from shell, handling -- MORE -- pager automatically.
        Keeps reading until no new data arrives for `settle` seconds.
        """
        buf = ""
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                if shell.recv_ready():
                    chunk = shell.recv(65535).decode(errors="replace")
                    buf += chunk
                    # If pager prompt detected → send space to continue
                    cleaned_so_far = _ANSI_RE.sub("", buf)
                    if re.search(r'--\s*[Mm]ore\s*--', cleaned_so_far):
                        dbg("           [PAGER] '-- More --' detected → sending space")
                        shell.send(" ")
                        time.sleep(0.3)
                    continue
                # No data ready – wait a bit then check if truly done
                time.sleep(0.2)
                if not shell.recv_ready():
                    time.sleep(settle)
                    if not shell.recv_ready():
                        break
            except socket.timeout:
                break
            except Exception:
                break
        return buf

    try:
        # Drain login banner
        dbg("           Draining login banner …")
        banner = _read_all(shell, settle=1.5, max_wait=10)
        dbg(f"           Banner received ({len(banner)} bytes)")

        # Send disable-pager command first (works on most OEMs)
        # Aruba/HP: terminal length 1000 (already in command list is fine,
        # but we also send it here to ensure paging is off before any cmd)
        dbg("           Sending terminal pager disable …")
        # shell.send("terminal length 1000\n")
        time.sleep(0.5)
        _read_all(shell, settle=0.5, max_wait=3)   # discard response

        # Run each command
        dbg("Step 4 – Sending commands one by one …")
        for i, cmd in enumerate(commands, 1):
            cmd = cmd.strip()
            if not cmd:
                continue
            dbg(f"           CMD {i}/{len(commands)}: {cmd}")
            shell.send(cmd + "\n")
            raw = _read_all(shell, settle=1.2, max_wait=timeout)
            clean = _clean(raw)
            dbg(f"           ✔ {len(raw)} raw bytes → {len(clean)} cleaned bytes")

            output_lines.append(f"{'─'*60}")
            output_lines.append(f"# {cmd}")
            output_lines.append(f"{'─'*60}")
            output_lines.append(clean or "(no output)")
            output_lines.append("")   # blank separator between commands

        shell.close()
        client.close()
        dbg("Step 4 – All commands completed successfully")
        return True, "\n".join(output_lines), debug_lines

    except Exception as e:
        dbg(f"           ✗ SHELL ERROR – {e}")
        dbg(traceback.format_exc())
        try:
            shell.close()
            client.close()
        except Exception:
            pass
        return False, f"Error while running commands: {e}", debug_lines



# ─────────────────────────────────────────────
#  Main Application GUI
# ─────────────────────────────────────────────
class BKPApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("BKP Application – Network Backup Tool")
        self.geometry("1100x620")
        self.minsize(900, 550)
        self.configure(bg="#1a1d2e")

        # State
        self.selected_oem    = tk.StringVar(value=OEM_LIST[0])
        self.username_var    = tk.StringVar()
        self.password_var    = tk.StringVar()
        self.ip_file_var     = tk.StringVar()
        self.backup_dir_var  = tk.StringVar()
        self.status_queue    = queue.Queue()
        self.running         = False
        self.debug_mode      = tk.BooleanVar(value=False)

        self._build_ui()
        self._poll_queue()

    # ─── UI Construction ───────────────────────
    def _build_ui(self):
        # Outer padding frame
        root_frame = tk.Frame(self, bg="#2b3ca0", padx=14, pady=14)
        root_frame.pack(fill="both", expand=True)

        # ── Title bar
        title_lbl = tk.Label(root_frame, text="BKP Application",
                             bg="#1a1d2e", fg="#e0e6f0",
                             font=("Courier New", 13, "bold"))
        title_lbl.pack(anchor="n", pady=(0, 8))

        # ── Main horizontal split
        main = tk.Frame(root_frame, bg="#1a1d2e")
        main.pack(fill="both", expand=True)

        # ── LEFT  – Status screen
        left = tk.Frame(main, bg="#e8eef2", bd=2, relief="groove")
        left.pack(side="left", fill="both", expand=True, padx=(0, 10))

        tk.Label(left, text="Status Screen", bg="#E6E8F6", fg="#5a6a8a",
                 font=("Courier New", 9)).pack(anchor="nw", padx=6, pady=3)

        self.status_box = scrolledtext.ScrolledText(
            left, bg="#0d1018", fg="#a8ffb0",
            font=("Courier New", 9), insertbackground="white",
            bd=0, wrap="word", state="disabled")
        self.status_box.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        # Colour tags
        self.status_box.tag_config("ok",      foreground="#3dffa0")
        self.status_box.tag_config("fail",    foreground="#ff4f4f")
        self.status_box.tag_config("warn",    foreground="#ffcc00")
        self.status_box.tag_config("info",    foreground="#5bc8f5")
        self.status_box.tag_config("header",  foreground="#ffffff", font=("Courier New", 9, "bold"))
        self.status_box.tag_config("debug",   foreground="#4868CA", font=("Courier New", 8))

        # ── RIGHT – Controls panel
        right = tk.Frame(main, bg="#1a1d2e", width=300)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        self._build_oem_selector(right)
        self._build_field(right, "User Name",              self.username_var,   show=None)
        self._build_field(right, "Password",               self.password_var,   show="●")
        self._build_browse(right, "Switch IP File",   self.ip_file_var,    mode="file")
        self._build_browse(right, "Backup Storing Directory", self.backup_dir_var, mode="dir")
        self._build_run_button(right)

    def _build_oem_selector(self, parent):
        frame = tk.Frame(parent, bg="#252840", bd=1, relief="groove")
        frame.pack(fill="x", pady=(0, 7))
        tk.Label(frame, text="Switch OEM", bg="#252840", fg="#8899cc",
                 font=("Courier New", 8, "bold")).pack(anchor="nw", padx=8, pady=(5, 2))
        oem_row = tk.Frame(frame, bg="#252840")
        oem_row.pack(fill="x", padx=8, pady=(0, 6))
        for oem in OEM_LIST:
            btn = tk.Radiobutton(
                oem_row, text=oem, variable=self.selected_oem, value=oem,
                bg="#252840", fg="#ccd6f0", selectcolor="#1a1d2e",
                activebackground="#252840", activeforeground="#ffffff",
                font=("Courier New", 9), indicatoron=True, bd=0,
                command=self._on_oem_change)
            btn.pack(side="left", padx=4)
        # OEM accent bar
        self.oem_bar = tk.Frame(frame, height=3, bg=OEM_COLORS[self.selected_oem.get()])
        self.oem_bar.pack(fill="x")

    def _on_oem_change(self):
        color = OEM_COLORS.get(self.selected_oem.get(), "#5bc8f5")
        self.oem_bar.configure(bg=color)

    def _build_field(self, parent, label: str, var: tk.StringVar, show=None):
        frame = tk.Frame(parent, bg="#252840", bd=1, relief="groove")
        frame.pack(fill="x", pady=(0, 7))
        tk.Label(frame, text=label, bg="#252840", fg="#8899cc",
                 font=("Courier New", 8, "bold")).pack(anchor="nw", padx=8, pady=(5, 1))
        entry = tk.Entry(frame, textvariable=var, bg="#181b2e", fg="#e0e6f0",
                         insertbackground="white", relief="flat",
                         font=("Courier New", 10), show=show or "")
        entry.pack(fill="x", padx=8, pady=(0, 6), ipady=4)

    def _build_browse(self, parent, label: str, var: tk.StringVar, mode: str):
        frame = tk.Frame(parent, bg="#252840", bd=1, relief="groove")
        frame.pack(fill="x", pady=(0, 7))
        tk.Label(frame, text=label, bg="#252840", fg="#8899cc",
                 font=("Courier New", 8, "bold")).pack(anchor="nw", padx=8, pady=(5, 1))
        row = tk.Frame(frame, bg="#252840")
        row.pack(fill="x", padx=8, pady=(0, 6))
        entry = tk.Entry(row, textvariable=var, bg="#181b2e", fg="#e0e6f0",
                         insertbackground="white", relief="flat",
                         font=("Courier New", 8))
        entry.pack(side="left", fill="x", expand=True, ipady=4)

        cmd = (lambda m=mode, v=var: self._browse(m, v))
        btn = tk.Button(row, text="…", command=cmd, bg="#3a3f6e", fg="#ffffff",
                        relief="flat", font=("Courier New", 9, "bold"),
                        padx=6, cursor="hand2")
        btn.pack(side="right", padx=(4, 0))

    def _build_run_button(self, parent):
        spacer = tk.Frame(parent, bg="#1a1d2e", height=10)
        spacer.pack()

        # ── Debug toggle
        debug_frame = tk.Frame(parent, bg="#1a1d2e")
        debug_frame.pack(fill="x", pady=(0, 6))

        self.debug_btn = tk.Checkbutton(
            debug_frame,
            text="🐛  Debug Mode",
            variable=self.debug_mode,
            command=self._on_debug_toggle,
            bg="#2a1f3d", fg="#b388ff",
            selectcolor="#1a1d2e",
            activebackground="#2a1f3d", activeforeground="#d0b0ff",
            font=("Courier New", 9, "bold"),
            relief="groove", bd=1,
            padx=8, pady=5,
            cursor="hand2",
            indicatoron=False,
            width=22)
        self.debug_btn.pack(fill="x")

        self.debug_label = tk.Label(
            debug_frame,
            text="  Off – only errors shown",
            bg="#1a1d2e", fg="#5a4a7a",
            font=("Courier New", 7))
        self.debug_label.pack(anchor="w", padx=4)

        # ── Run / Stop
        self.run_btn = tk.Button(
            parent, text="▶  RUN BACKUP",
            command=self._start_backup,
            bg="#1db954", fg="#000000", activebackground="#17a348",
            font=("Courier New", 10, "bold"), relief="flat",
            padx=10, pady=8, cursor="hand2")
        self.run_btn.pack(fill="x", pady=(0, 4))

        self.stop_btn = tk.Button(
            parent, text="■  STOP",
            command=self._stop_backup,
            bg="#e84c34", fg="#ffffff", activebackground="#e84c34",
            font=("Courier New", 10, "bold"), relief="flat",
            padx=10, pady=8, cursor="hand2", state="disabled")
        self.stop_btn.pack(fill="x")

    # ─── Browse helpers ────────────────────────
    def _browse(self, mode: str, var: tk.StringVar):
        if mode == "file":
            path = filedialog.askopenfilename(
                title="Select Switch IP list file",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        else:
            path = filedialog.askdirectory(title="Select Backup Directory")
        if path:
            var.set(path)

    def _on_debug_toggle(self):
        if self.debug_mode.get():
            self.debug_btn.configure(bg="#4a2f7a", fg="#ffffff")
            self.debug_label.configure(
                text="  On – full SSH diagnostics shown", fg="#b388ff")
            self._log("─── Debug mode ENABLED ──────────────────────────────", "debug")
            self._log("  Each switch will show step-by-step SSH diagnostics.", "debug")
            self._log("  Purple lines = debug info. Errors explained in detail.", "debug")
            self._log("─────────────────────────────────────────────────────", "debug")
        else:
            self.debug_btn.configure(bg="#2a1f3d", fg="#b388ff")
            self.debug_label.configure(
                text="  Off – only errors shown", fg="#5a4a7a")
            self._log("─── Debug mode DISABLED ─────────────────────────────", "debug")

    # ─── Status helpers ────────────────────────
    def _log(self, text: str, tag: str = "info"):
        self.status_queue.put((text, tag))

    def _poll_queue(self):
        try:
            while True:
                text, tag = self.status_queue.get_nowait()
                self.status_box.configure(state="normal")
                self.status_box.insert("end", text + "\n", tag)
                self.status_box.see("end")
                self.status_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _clear_status(self):
        self.status_box.configure(state="normal")
        self.status_box.delete("1.0", "end")
        self.status_box.configure(state="disabled")

    # ─── Validation & command loading ──────────
    def _load_commands(self, oem: str) -> tuple[bool, list[str]]:
        """
        Look for <oem>.txt in the current working directory.
        Returns (found, commands_list).
        """
        filename = f"{oem.lower()}.txt"
        filepath = Path(os.getcwd()) / filename

        if not filepath.exists():
            # Create a template file so user knows what to edit
            try:
                filepath.write_text(
                    f"# Commands for {oem} – edit this file then re-run\n"
                    + OEM_DEFAULT_COMMANDS.get(oem.lower(), "show version\n"),
                    encoding="utf-8")
            except Exception:
                pass
            return False, []

        lines = filepath.read_text(encoding="utf-8").splitlines()
        commands = [l for l in lines if l.strip() and not l.strip().startswith("#")]
        if not commands:
            return False, []
        return True, commands

    def _validate_inputs(self) -> bool:
        errs = []
        if not self.username_var.get().strip():
            errs.append("• User Name is required.")
        if not self.password_var.get().strip():
            errs.append("• Password is required.")
        if not self.ip_file_var.get().strip():
            errs.append("• Switch IP Directory (file) is required.")
        elif not os.path.isfile(self.ip_file_var.get().strip()):
            errs.append("• Switch IP file not found.")
        if not self.backup_dir_var.get().strip():
            errs.append("• Backup Storing Directory is required.")
        elif not os.path.isdir(self.backup_dir_var.get().strip()):
            errs.append("• Backup directory does not exist.")
        if errs:
            messagebox.showerror("Validation Error", "\n".join(errs))
            return False
        return True

    # ─── Run / Stop logic ──────────────────────
    def _start_backup(self):
        if self.running:
            return
        if not self._validate_inputs():
            return

        oem      = self.selected_oem.get()
        ok, cmds = self._load_commands(oem)

        self._clear_status()

        if not ok:
            self._log(f"[ERROR] No command file found for '{oem}'.", "fail")
            self._log(f"        Expected: {oem.lower()}.txt in {os.getcwd()}", "warn")
            self._log("        A template file has been created. Edit it and re-run.", "warn")
            return

        # Load IPs
        try:
            ip_lines = Path(self.ip_file_var.get().strip()).read_text(
                encoding="utf-8").splitlines()
            ips = [l.strip() for l in ip_lines if l.strip() and not l.startswith("#")]
        except Exception as exc:
            self._log(f"[ERROR] Cannot read IP file: {exc}", "fail")
            return

        if not ips:
            self._log("[ERROR] IP file is empty or has no valid IPs.", "fail")
            return

        self.running = True
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        username   = self.username_var.get().strip()
        password   = self.password_var.get().strip()
        backup_dir = self.backup_dir_var.get().strip()
        max_threads = 10

        self._log(f"{'═'*56}", "header")
        self._log(f"  BKP Run  │  OEM: {oem}  │  {datetime.now():%Y-%m-%d %H:%M}", "header")
        self._log(f"  Switches : {len(ips)}  │  Commands: {len(cmds)}", "header")
        self._log(f"{'═'*56}", "header")

        thread = threading.Thread(
            target=self._worker_pool,
            args=(ips, username, password, cmds, backup_dir, max_threads),
            daemon=True)
        thread.start()

    def _stop_backup(self):
        self.running = False
        self._log("[STOPPED] User requested stop. Finishing active threads…", "warn")

    def _worker_pool(self, ips, username, password, commands, backup_dir, max_threads):
        sem     = threading.Semaphore(max_threads)
        threads = []
        results = {"ok": 0, "fail": 0}
        lock    = threading.Lock()

        def worker(ip):
            with sem:
                if not self.running:
                    return
                ts = datetime.now().strftime("%H:%M:%S")
                self._log(f"[{ts}] ⟳  {ip} – connecting …", "info")
                success, output, dbg_lines = ssh_run_commands(
                    ip, username, password, commands,
                    debug=self.debug_mode.get())

                # Always show debug lines if debug mode on
                if self.debug_mode.get():
                    self._log(f"  ┌── Debug trace for {ip}", "debug")
                    for dl in dbg_lines:
                        self._log(dl, "debug")
                    self._log(f"  └── End trace for {ip}", "debug")

                with lock:
                    if success:
                        results["ok"] += 1
                    else:
                        results["fail"] += 1

                ts = datetime.now().strftime("%H:%M:%S")
                if success:
                    # Save backup
                    safe_ip   = ip.replace(".", "_").replace(":", "_")
                    out_path  = os.path.join(backup_dir, f"{safe_ip}.txt")
                    header    = (f"# IP      : {ip}\n"
                                 f"# OEM     : {self.selected_oem.get()}\n"
                                 f"# Date    : {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                                 f"# User    : {username}\n"
                                 + "─" * 60 + "\n")
                    try:
                        Path(out_path).write_text(header + output, encoding="utf-8")
                        self._log(f"[{ts}] ✔  {ip} – SUCCESS → {os.path.basename(out_path)}", "ok")
                    except Exception as exc:
                        self._log(f"[{ts}] ✔  {ip} – commands OK but save failed: {exc}", "warn")
                else:
                    self._log(f"[{ts}] ✗  {ip} – FAILED: {output}", "fail")
                    if not self.debug_mode.get():
                        self._log(f"         💡 Tip: Enable Debug Mode and re-run for full diagnostics.", "warn")

        for ip in ips:
            t = threading.Thread(target=worker, args=(ip,), daemon=True)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Summary
        self._log(f"\n{'═'*56}", "header")
        self._log(f"  DONE  │  ✔ {results['ok']} succeeded  │  ✗ {results['fail']} failed", "header")
        self._log(f"{'═'*56}\n", "header")
        self.running = False
        self.after(0, lambda: self.run_btn.configure(state="normal"))
        self.after(0, lambda: self.stop_btn.configure(state="disabled"))


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    app = BKPApp()
    app.mainloop()