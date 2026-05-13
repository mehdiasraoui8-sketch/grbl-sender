import threading
import queue
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import serial
from serial.tools import list_ports


DEFAULT_BAUD = 115200
STATUS_POLL_MS = 200


class GrblSenderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("GRBL G-code Sender")

        self.serial = None
        self.reader_thread = None
        self.reader_running = False

        self.rx_queue = queue.Queue()
        self.tx_queue = queue.Queue()
        self.awaiting_ok = False
        self.streaming = False

        self._build_ui()
        self._refresh_ports()
        self._process_rx_queue()

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.grid(row=0, column=0, sticky="nsew")

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        # Connection row
        conn = ttk.Frame(top)
        conn.grid(row=0, column=0, sticky="ew")
        conn.columnconfigure(1, weight=1)

        ttk.Label(conn, text="Port:").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(conn, textvariable=self.port_var, state="readonly")
        self.port_combo.grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(conn, text="Refresh", command=self._refresh_ports).grid(row=0, column=2, padx=4)

        ttk.Label(conn, text="Baud:").grid(row=0, column=3, sticky="e")
        self.baud_var = tk.StringVar(value=str(DEFAULT_BAUD))
        self.baud_entry = ttk.Entry(conn, textvariable=self.baud_var, width=8)
        self.baud_entry.grid(row=0, column=4, padx=4)

        self.connect_btn = ttk.Button(conn, text="Connect", command=self._connect)
        self.connect_btn.grid(row=0, column=5, padx=4)
        self.disconnect_btn = ttk.Button(conn, text="Disconnect", command=self._disconnect, state="disabled")
        self.disconnect_btn.grid(row=0, column=6, padx=4)

        # Status row
        status = ttk.Frame(top)
        status.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        status.columnconfigure(1, weight=1)

        ttk.Label(status, text="Status:").grid(row=0, column=0, sticky="w")
        self.status_var = tk.StringVar(value="Disconnected")
        ttk.Label(status, textvariable=self.status_var).grid(row=0, column=1, sticky="w")

        # Jog + control row
        controls = ttk.Frame(top)
        controls.grid(row=2, column=0, sticky="ew", pady=(6, 0))

        self.step_var = tk.StringVar(value="1.0")
        self.feed_var = tk.StringVar(value="300")
        ttk.Label(controls, text="Step (mm):").grid(row=0, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.step_var, width=6).grid(row=0, column=1, padx=4)
        ttk.Label(controls, text="Feed (mm/min):").grid(row=0, column=2, sticky="w")
        ttk.Entry(controls, textvariable=self.feed_var, width=8).grid(row=0, column=3, padx=4)

        ttk.Button(controls, text="X-", command=lambda: self._jog("X", -1)).grid(row=0, column=4, padx=2)
        ttk.Button(controls, text="X+", command=lambda: self._jog("X", 1)).grid(row=0, column=5, padx=2)
        ttk.Button(controls, text="Y-", command=lambda: self._jog("Y", -1)).grid(row=0, column=6, padx=2)
        ttk.Button(controls, text="Y+", command=lambda: self._jog("Y", 1)).grid(row=0, column=7, padx=2)
        ttk.Button(controls, text="Z-", command=lambda: self._jog("Z", -1)).grid(row=0, column=8, padx=2)
        ttk.Button(controls, text="Z+", command=lambda: self._jog("Z", 1)).grid(row=0, column=9, padx=2)

        ttk.Button(controls, text="Home ($H)", command=lambda: self._send_line("$H")).grid(row=0, column=10, padx=4)
        ttk.Button(controls, text="Unlock ($X)", command=lambda: self._send_line("$X")).grid(row=0, column=11, padx=4)
        hold_btn = ttk.Button(controls, text="Hold (!)", command=lambda: self._send_realtime(b"!"))
        hold_btn.grid(row=0, column=12, padx=2)
        resume_btn = ttk.Button(controls, text="Resume (~)", command=lambda: self._send_realtime(b"~"))
        resume_btn.grid(row=0, column=13, padx=2)
        reset_btn = ttk.Button(controls, text="Reset (Ctrl-X)", command=lambda: self._send_realtime(b"\x18"))
        reset_btn.grid(row=0, column=14, padx=2)

        # Overrides row
        overrides = ttk.Frame(top)
        overrides.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(overrides, text="Feed Override:").grid(row=0, column=0, sticky="w")
        ttk.Button(overrides, text="-10%", command=lambda: self._send_realtime(b"\x93")).grid(row=0, column=1, padx=2)
        ttk.Button(overrides, text="+10%", command=lambda: self._send_realtime(b"\x91")).grid(row=0, column=2, padx=2)
        ttk.Button(overrides, text="100%", command=lambda: self._send_realtime(b"\x90")).grid(row=0, column=3, padx=2)

        # File row
        file_row = ttk.Frame(top)
        file_row.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        file_row.columnconfigure(1, weight=1)

        ttk.Label(file_row, text="G-code file:").grid(row=0, column=0, sticky="w")
        self.file_var = tk.StringVar()
        ttk.Entry(file_row, textvariable=self.file_var).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(file_row, text="Browse", command=self._browse_file).grid(row=0, column=2, padx=4)
        self.send_btn = ttk.Button(file_row, text="Send", command=self._start_stream, state="disabled")
        self.send_btn.grid(row=0, column=3, padx=4)
        self.stop_btn = ttk.Button(file_row, text="Stop", command=self._stop_stream, state="disabled")
        self.stop_btn.grid(row=0, column=4, padx=4)

        # Console
        console_frame = ttk.Frame(top)
        console_frame.grid(row=5, column=0, sticky="nsew", pady=(6, 0))
        console_frame.rowconfigure(0, weight=1)
        console_frame.columnconfigure(0, weight=1)

        self.console = tk.Text(console_frame, height=18, wrap="word")
        self.console.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(console_frame, orient="vertical", command=self.console.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.console.configure(yscrollcommand=scrollbar.set)

    def _refresh_ports(self):
        ports = [p.device for p in list_ports.comports()]
        self.port_combo["values"] = ports
        if ports:
            self.port_combo.current(0)
        self._log(f"Available ports: {ports if ports else 'None detected'}")

    def _connect(self):
        if self.serial:
            return
        port = self.port_var.get()
        if not port:
            messagebox.showerror("Error", "Select a serial port.")
            return
        try:
            baud = int(self.baud_var.get())
        except ValueError:
            messagebox.showerror("Error", "Invalid baud rate.")
            return

        try:
            self._log(f"Attempting to connect to {port} at {baud} baud...")
            self.serial = serial.Serial(
                port,
                baudrate=baud,
                timeout=0.1,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                bytesize=serial.EIGHTBITS,
                rtscts=False,
                dsrdtr=False
            )
            
            time.sleep(0.2)
            self._log("Connected successfully! Initializing...")
            
        except serial.SerialException as exc:
            self._log(f"ERROR: Failed to open port: {exc}")
            messagebox.showerror("Error", f"Failed to open port: {exc}")
            return

        time.sleep(2)

        self.reader_running = True
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

        self.connect_btn.config(state="disabled")
        self.disconnect_btn.config(state="normal")
        self.send_btn.config(state="normal")
        self.status_var.set("Connected")
        
        time.sleep(0.5)
        self._send_realtime(b"?")

        self.root.after(STATUS_POLL_MS, self._poll_status)

    def _disconnect(self):
        self._stop_stream()
        self.reader_running = False
        if self.reader_thread:
            self.reader_thread.join(timeout=1.0)
        if self.serial:
            self.serial.close()
            self.serial = None

        self.connect_btn.config(state="normal")
        self.disconnect_btn.config(state="disabled")
        self.send_btn.config(state="disabled")
        self.status_var.set("Disconnected")
        self._log("Disconnected")

    def _reader_loop(self):
        while self.reader_running and self.serial:
            try:
                if self.serial.in_waiting:
                    line = self.serial.readline().decode("utf-8", errors="ignore").strip()
                    if line:
                        self.rx_queue.put(line)
            except serial.SerialException as e:
                self._log(f"Serial error: {e}")
                break
            except Exception as e:
                self._log(f"Unexpected error in reader: {e}")
                break
            time.sleep(0.01)

    def _process_rx_queue(self):
        while not self.rx_queue.empty():
            line = self.rx_queue.get()
            self._log(line)
            if line.startswith("<"):
                self.status_var.set(line)
            if line.lower().startswith("ok") or line.lower().startswith("error"):
                self.awaiting_ok = False
                self._send_next_from_queue()
        self.root.after(50, self._process_rx_queue)

    def _poll_status(self):
        if self.serial:
            self._send_realtime(b"?")
            self.root.after(STATUS_POLL_MS, self._poll_status)

    def _send_realtime(self, data):
        if not self.serial:
            return
        try:
            self.serial.write(data)
            self.serial.flush()
        except serial.SerialException as e:
            self._log(f"Error sending data: {e}")

    def _send_line(self, line):
        if not self.serial:
            return
        if self.streaming:
            self.tx_queue.put(line)
            if not self.awaiting_ok:
                self._send_next_from_queue()
            return
        self._write_line(line)

    def _write_line(self, line):
        if not self.serial:
            return
        try:
            payload = (line.strip() + "\n").encode("ascii")
            self.serial.write(payload)
            self.serial.flush()
            self.awaiting_ok = True
        except serial.SerialException as e:
            self._log(f"Error writing line: {e}")

    def _send_next_from_queue(self):
        if self.awaiting_ok:
            return
        if self.tx_queue.empty():
            if self.streaming:
                self.streaming = False
                self.stop_btn.config(state="disabled")
                self.send_btn.config(state="normal")
                self._log("-- Stream complete --")
            return
        line = self.tx_queue.get()
        if line:
            self._write_line(line)

    def _browse_file(self):
        path = filedialog.askopenfilename(filetypes=[("G-code", "*.gcode *.nc *.txt"), ("All files", "*")])
        if path:
            self.file_var.set(path)

    def _start_stream(self):
        if not self.serial:
            messagebox.showerror("Error", "Connect to a serial port first.")
            return
        path = self.file_var.get()
        if not path:
            messagebox.showerror("Error", "Select a G-code file.")
            return

        try:
            with open(path, "r", encoding="ascii", errors="ignore") as f:
                lines = [self._sanitize_gcode_line(l) for l in f.readlines()]
        except OSError as exc:
            messagebox.showerror("Error", f"Failed to read file: {exc}")
            return

        lines = [l for l in lines if l]
        if not lines:
            messagebox.showerror("Error", "No valid G-code lines found.")
            return

        self.streaming = True
        self.send_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._log(f"-- Streaming {len(lines)} lines --")

        for line in lines:
            self.tx_queue.put(line)

        if not self.awaiting_ok:
            self._send_next_from_queue()

    def _stop_stream(self):
        self.streaming = False
        while not self.tx_queue.empty():
            try:
                self.tx_queue.get_nowait()
            except queue.Empty:
                break
        self.stop_btn.config(state="disabled")
        if self.serial:
            self.send_btn.config(state="normal")
        self._log("-- Stream stopped --")

    def _sanitize_gcode_line(self, line):
        # Remove comments after semicolon
        if ';' in line:
            line = line[:line.index(';')]
        
        line = line.strip()
        if not line:
            return ""
        
        # Remove parentheses comments
        if line.startswith("(") and line.endswith(")"):
            return ""
        
        return line

    def _jog(self, axis, direction):
        """
        Jog using G0 (rapid) or G1 (linear) commands instead of $J.
        GRBL 0.9i does not support $J. Use standard G-code instead.
        """
        if not self.serial:
            self._log("ERROR: Not connected to serial port")
            messagebox.showerror("Error", "Connect to a serial port first.")
            return
        
        try:
            step_str = self.step_var.get().strip()
            feed_str = self.feed_var.get().strip()
            
            # Remove any commas and spaces
            step_str = step_str.replace(',', '.').replace(' ', '')
            feed_str = feed_str.replace(',', '.').replace(' ', '')
            
            step = float(step_str)
            feed = float(feed_str)
            
            if step <= 0 or feed <= 0:
                raise ValueError("Step and Feed must be positive numbers")
                
        except ValueError as e:
            error_msg = f"Invalid step or feed value: {e}\nStep (mm): {self.step_var.get()}\nFeed (mm/min): {self.feed_var.get()}"
            self._log(f"ERROR: {error_msg}")
            messagebox.showerror("Error", error_msg)
            return
        
        distance = step * direction
        
        # Use G0 (rapid move) for jogging - GRBL 0.9i compatible
        # Format: G0 X<value> Y<value> Z<value> F<feed>
        # We send the command with only the axis being moved
        cmd = f"G0 {axis}{distance:.2f} F{feed:.0f}"
        
        self._log(f"Sending jog command: {cmd}")
        self._send_line(cmd)

    def _log(self, text):
        self.console.insert("end", text + "\n")
        self.console.see("end")


if __name__ == "__main__":
    root = tk.Tk()
    app = GrblSenderApp(root)
    root.mainloop()
