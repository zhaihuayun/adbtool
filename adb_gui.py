"""用按钮完成 ADB 启动、上传和下载。默认把文件传到设备的 /data。"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import sys
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

ADB_PATH = Path(r"D:\adb\adb.exe")
DEFAULT_REMOTE = "/data"
APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / "settings.json"
ERROR_LOG = APP_DIR / "error.log"

READY_STATES = {"device", "recovery", "sideload"}
STATE_TEXT = {
    "device": "已连接",
    "unauthorized": "未授权",
    "offline": "离线",
    "recovery": "Recovery",
    "sideload": "Sideload",
    "bootloader": "Bootloader",
    "rescue": "Rescue",
    "host": "Host",
    "connecting": "连接中",
    "no permissions": "无权限",
}


def enable_dpi() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def hidden_subprocess_kwargs() -> dict:
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return {
        "startupinfo": startup,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def norm_remote(path: str) -> str:
    raw = (path or "").strip().replace("\\", "/")
    if not raw:
        return DEFAULT_REMOTE
    if not raw.startswith("/"):
        raw = "/" + raw
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts) if parts else "/"


def remote_join(directory: str, name: str) -> str:
    directory = norm_remote(directory)
    if directory == "/":
        return "/" + name
    return directory.rstrip("/") + "/" + name


def remote_parent(path: str) -> str:
    path = norm_remote(path)
    if path == "/":
        return "/"
    parent, _sep, _name = path.rpartition("/")
    return parent or "/"


def parse_devices(text: str) -> list[dict]:
    devices: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("*") or line.startswith("List of"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial = parts[0]
        if parts[1] == "no" and len(parts) > 2 and parts[2] == "permissions":
            state = "no permissions"
            extra = parts[3:]
        else:
            state = parts[1]
            extra = parts[2:]
        if state not in STATE_TEXT:
            continue
        model = ""
        for item in extra:
            if item.startswith("model:"):
                model = item.split(":", 1)[1]
        devices.append({"serial": serial, "state": state, "model": model})
    return devices


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def parse_ls(text: str) -> list[tuple[str, bool]]:
    entries: list[tuple[str, bool]] = []
    for raw in text.splitlines():
        name = ANSI_RE.sub("", raw).strip()
        if not name or name.lower().startswith("ls:"):
            continue
        is_dir = name.endswith("/")
        if is_dir:
            name = name[:-1]
        if name in {"", ".", ".."}:
            continue
        entries.append((name, is_dir))
    entries.sort(key=lambda item: (not item[1], item[0].lower()))
    return entries


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(data: dict) -> None:
    try:
        SETTINGS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def write_error(text: str) -> None:
    try:
        with ERROR_LOG.open("a", encoding="utf-8") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
    except OSError:
        pass


class AdbGui(tk.Tk):
    def __init__(self, autostart: bool = True) -> None:
        super().__init__()
        self.title("ADB 文件传输")
        self.configure(bg="#eef2f7")
        self.minsize(920, 580)
        self.settings = load_settings()
        self.adb = str(ADB_PATH)
        self.devices: list[dict] = []
        self.local_files: list[str] = []
        self.remote_entries: list[dict] = []
        self._queue: queue.Queue = queue.Queue()
        self._job_lock = threading.Lock()
        self._busy = False
        self._applying_devices = False
        self._after_id: str | None = None
        self._last_download = self.settings.get("last_download_dir") or self._default_download_dir()
        self._default_hint = "默认上传到设备的 /data。先启动 ADB，再打开本地文件。"

        self.remote_path = tk.StringVar(value=DEFAULT_REMOTE)
        self.hint = tk.StringVar(value=self._default_hint)
        self.status = tk.StringVar(value="就绪")
        self.local_hint = tk.StringVar(value="待上传  0 个文件")
        self.remote_hint = tk.StringVar(value=DEFAULT_REMOTE)

        self._build()
        self._place_window()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_id = self.after(80, self._poll)
        self._log("ADB 路径：" + self.adb)
        self._log("上传默认目录：/data")
        self._log("流程：启动 ADB → 打开（选择文件）→ 上传。右侧选中设备文件后点下载。")
        if not ADB_PATH.is_file():
            self._log("没有找到 adb.exe，请确认 D:\\adb\\adb.exe 存在。")
            messagebox.showerror("找不到 ADB", f"没有找到：\n{ADB_PATH}", parent=self)
            return
        if autostart:
            self.after(200, self.on_refresh_devices)

    def _default_download_dir(self) -> str:
        downloads = Path.home() / "Downloads"
        if downloads.is_dir():
            return str(downloads)
        return str(APP_DIR)

    def _place_window(self) -> None:
        self.update_idletasks()
        width, height = 1100, 720
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build(self) -> None:
        header = tk.Frame(self, bg="#111827", padx=16, pady=12)
        header.pack(fill="x")
        tk.Label(
            header,
            text="ADB 文件传输",
            bg="#111827",
            fg="white",
            font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(side="left")
        tk.Label(
            header,
            text="默认目录  /data",
            bg="#111827",
            fg="#9ca3af",
            font=("Microsoft YaHei UI", 10),
        ).pack(side="left", padx=(12, 0), pady=(4, 0))

        toolbar = tk.Frame(self, bg="#eef2f7", padx=12, pady=10)
        toolbar.pack(fill="x")
        self.btn_start = self._button(toolbar, "启动 ADB", self.on_start_adb, "#0f766e")
        self.btn_start.pack(side="left")
        self.btn_refresh_devices = self._button(
            toolbar, "刷新设备", self.on_refresh_devices, "#e5e7eb", "#111827"
        )
        self.btn_refresh_devices.pack(side="left", padx=(8, 0))
        tk.Label(
            toolbar,
            text="设备",
            bg="#eef2f7",
            fg="#374151",
            font=("Microsoft YaHei UI", 10),
        ).pack(side="left", padx=(16, 6))
        self.device_combo = ttk.Combobox(toolbar, state="readonly", width=48, font=("Microsoft YaHei UI", 10))
        self.device_combo.pack(side="left", fill="x", expand=True)
        self.device_combo.bind("<<ComboboxSelected>>", self.on_device_changed)

        path_bar = tk.Frame(self, bg="#eef2f7", padx=12)
        path_bar.pack(fill="x")
        tk.Label(
            path_bar,
            text="设备目录",
            bg="#eef2f7",
            fg="#374151",
            font=("Microsoft YaHei UI", 10),
        ).pack(side="left")
        self.path_entry = tk.Entry(
            path_bar,
            textvariable=self.remote_path,
            font=("Consolas", 11),
            relief="flat",
            highlightthickness=1,
            highlightbackground="#d1d5db",
            highlightcolor="#2563eb",
        )
        self.path_entry.pack(side="left", fill="x", expand=True, padx=8, ipady=4)
        self.path_entry.bind("<Return>", lambda _event: self.on_refresh_remote())
        self.btn_go = self._button(path_bar, "转到", self.on_refresh_remote, "#1f2937")
        self.btn_go.pack(side="left")
        self.btn_up = self._button(path_bar, "上级", self.on_up, "#e5e7eb", "#111827")
        self.btn_up.pack(side="left", padx=(8, 0))
        self.quick_buttons = []
        for label, path in (("/data", "/data"), ("/sdcard", "/sdcard"), ("临时目录", "/data/local/tmp")):
            button = self._button(
                path_bar,
                label,
                lambda p=path: self.on_jump(p),
                "#e5e7eb",
                "#111827",
            )
            button.pack(side="left", padx=(8, 0))
            self.quick_buttons.append(button)

        body = tk.PanedWindow(self, orient="horizontal", sashwidth=6, bg="#d1d5db", bd=0)
        body.pack(fill="both", expand=True, padx=12, pady=10)

        left = tk.Frame(body, bg="white", padx=10, pady=10)
        right = tk.Frame(body, bg="white", padx=10, pady=10)
        body.add(left, stretch="always", minsize=360)
        body.add(right, stretch="always", minsize=360)

        tk.Label(
            left,
            text="本地文件",
            bg="white",
            fg="#111827",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(anchor="w")
        tk.Label(
            left,
            textvariable=self.local_hint,
            bg="white",
            fg="#6b7280",
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(0, 6))
        local_frame, self.local_list = self._listbox(left)
        local_frame.pack(fill="both", expand=True)
        self.local_list.bind("<Delete>", lambda _event: self.on_remove_local())

        left_actions = tk.Frame(left, bg="white")
        left_actions.pack(fill="x", pady=(8, 0))
        self.btn_open = self._button(left_actions, "打开", self.on_open, "#1d4ed8")
        self.btn_open.pack(side="left")
        self.btn_remove = self._button(left_actions, "移除", self.on_remove_local, "#e5e7eb", "#111827")
        self.btn_remove.pack(side="left", padx=(8, 0))
        self.btn_clear = self._button(left_actions, "清空", self.on_clear_local, "#e5e7eb", "#111827")
        self.btn_clear.pack(side="left", padx=(8, 0))
        self.btn_upload = self._button(left_actions, "上传", self.on_upload, "#15803d")
        self.btn_upload.pack(side="right")

        tk.Label(
            right,
            text="设备文件",
            bg="white",
            fg="#111827",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(anchor="w")
        tk.Label(
            right,
            textvariable=self.remote_hint,
            bg="white",
            fg="#6b7280",
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(0, 6))
        remote_frame, self.remote_list = self._listbox(right)
        remote_frame.pack(fill="both", expand=True)
        self.remote_list.bind("<Double-Button-1>", self.on_remote_activate)
        self.remote_list.bind("<Return>", self.on_remote_activate)
        self.remote_list.bind("<Delete>", lambda _event: self.on_delete_remote())

        right_actions = tk.Frame(right, bg="white")
        right_actions.pack(fill="x", pady=(8, 0))
        self.btn_download = self._button(right_actions, "下载", self.on_download, "#b45309")
        self.btn_download.pack(side="left")
        self.btn_enter = self._button(right_actions, "进入", self.on_enter, "#e5e7eb", "#111827")
        self.btn_enter.pack(side="left", padx=(8, 0))
        self.btn_refresh_remote = self._button(
            right_actions, "刷新目录", self.on_refresh_remote, "#e5e7eb", "#111827"
        )
        self.btn_refresh_remote.pack(side="left", padx=(8, 0))
        self.btn_delete_remote = self._button(
            right_actions, "删除", self.on_delete_remote, "#dc2626"
        )
        self.btn_delete_remote.pack(side="left", padx=(8, 0))

        log_wrap = tk.Frame(self, bg="#eef2f7", padx=12)
        log_wrap.pack(fill="x")
        tk.Label(
            log_wrap,
            text="日志",
            bg="#eef2f7",
            fg="#374151",
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w")
        log_box = tk.Frame(log_wrap, bg="#0f172a")
        log_box.pack(fill="x", pady=(2, 0))
        scroll = ttk.Scrollbar(log_box, orient="vertical")
        self.log = tk.Text(
            log_box,
            height=8,
            bg="#0f172a",
            fg="#e2e8f0",
            insertbackground="#e2e8f0",
            font=("Consolas", 9),
            relief="flat",
            wrap="word",
            state="disabled",
            yscrollcommand=scroll.set,
        )
        scroll.config(command=self.log.yview)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        footer = tk.Frame(self, bg="#e5e7eb", padx=12, pady=6)
        footer.pack(fill="x", side="bottom")
        tk.Label(
            footer,
            textvariable=self.status,
            bg="#e5e7eb",
            fg="#111827",
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left")
        tk.Label(
            footer,
            textvariable=self.hint,
            bg="#e5e7eb",
            fg="#4b5563",
            font=("Microsoft YaHei UI", 9),
        ).pack(side="right")

        self._bind_hint(self.btn_start, "启动 ADB 服务，并列出已连接的设备")
        self._bind_hint(self.btn_open, "选择要上传的本地文件，可多选")
        self._bind_hint(self.btn_upload, "把左侧文件上传到上方的设备目录，默认 /data")
        self._bind_hint(self.btn_download, "把右侧选中的设备文件下载到电脑")
        self._bind_hint(self.btn_delete_remote, "删除右侧选中的设备文件或目录，支持多选")

        self.action_buttons = [
            self.btn_start,
            self.btn_refresh_devices,
            self.btn_go,
            self.btn_up,
            self.btn_open,
            self.btn_remove,
            self.btn_clear,
            self.btn_upload,
            self.btn_download,
            self.btn_enter,
            self.btn_refresh_remote,
            self.btn_delete_remote,
            *self.quick_buttons,
        ]

    def _button(self, parent, text, command, bg, fg="white"):
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=bg,
            activeforeground=fg,
            relief="flat",
            padx=12,
            pady=4,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10, "bold"),
            bd=0,
        )

    def _listbox(self, parent):
        frame = tk.Frame(parent, bg="white")
        y_scroll = ttk.Scrollbar(frame, orient="vertical")
        x_scroll = ttk.Scrollbar(frame, orient="horizontal")
        widget = tk.Listbox(
            frame,
            selectmode="extended",
            exportselection=False,
            activestyle="none",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#e5e7eb",
            font=("Microsoft YaHei UI", 10),
            bg="white",
            fg="#111827",
            selectbackground="#bfdbfe",
            selectforeground="#111827",
            yscrollcommand=y_scroll.set,
            xscrollcommand=x_scroll.set,
        )
        y_scroll.config(command=widget.yview)
        x_scroll.config(command=widget.xview)
        widget.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return frame, widget

    def _bind_hint(self, widget, text: str) -> None:
        widget.bind("<Enter>", lambda _event: self.hint.set(text))
        widget.bind("<Leave>", lambda _event: self.hint.set(self._default_hint))

    def _on_close(self) -> None:
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
        self.settings["last_download_dir"] = self._last_download
        save_settings(self.settings)
        self.destroy()

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                else:
                    payload()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self._after_id = self.after(80, self._poll)

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > 400:
            self.log.delete("1.0", f"{line_count - 400}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def post(self, text: str) -> None:
        self._queue.put(("log", text))

    def ui(self, callback) -> None:
        self._queue.put(("ui", callback))

    def run_job(self, title: str, func) -> None:
        if not self._job_lock.acquire(blocking=False):
            messagebox.showinfo("请稍候", "当前有任务正在执行，请等它结束后再操作。", parent=self)
            return
        self._set_busy(True, title)

        def wrapper() -> None:
            final_status = "就绪"
            try:
                result = func()
                if isinstance(result, str) and result:
                    final_status = result
            except Exception as exc:
                detail = traceback.format_exc()
                write_error(detail)
                self.post(f"执行失败：{exc}")
                final_status = "执行失败"
            finally:
                self._job_lock.release()
                self.ui(lambda status=final_status: self._set_busy(False, status))

        threading.Thread(target=wrapper, daemon=True).start()

    def _set_busy(self, busy: bool, text: str) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in self.action_buttons:
            button.configure(state=state)
        self.device_combo.configure(state="disabled" if busy else "readonly")
        self.status.set(text)

    def capture(self, args: list[str], timeout: int = 30) -> tuple[int, str]:
        self.post("$ " + subprocess.list2cmdline(args))
        try:
            completed = subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
                **hidden_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired:
            self.post("命令超时")
            return 124, "命令超时"
        text = (completed.stdout or b"").decode("utf-8", errors="replace")
        return completed.returncode, text

    def stream(self, args: list[str]) -> int:
        self.post("$ " + subprocess.list2cmdline(args))
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            **hidden_subprocess_kwargs(),
        )
        assert process.stdout is not None
        buffer = b""
        last_percent: str | None = None
        while True:
            chunk = process.stdout.read(1024)
            if not chunk:
                break
            buffer += chunk
            parts = re.split(br"\r\n|\n|\r", buffer)
            buffer = parts.pop()
            for part in parts:
                last_percent = self._emit_output(part, last_percent)
        if buffer.strip():
            self._emit_output(buffer, last_percent)
        code = process.wait()
        if code != 0:
            self.post(f"命令退出码 {code}")
        return code

    def _emit_output(self, raw: bytes, last_percent: str | None) -> str | None:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return last_percent
        matched = re.search(r"\[\s*(\d+)%\]", text)
        if matched:
            percent = matched.group(1)
            self.ui(lambda p=percent, line=text: self.status.set(f"进度 {p}%  {line}"))
            if (
                last_percent is None
                or abs(int(percent) - int(last_percent)) >= 10
                or percent == "100"
            ):
                self.post(text)
            return percent
        self.post(text)
        return last_percent

    def _query_devices(self) -> list[dict]:
        code, out = self.capture([self.adb, "devices", "-l"])
        for line in out.splitlines():
            if line.strip().startswith("*"):
                self.post(line.strip())
        if code != 0:
            self.post(out.strip() or "读取设备列表失败")
            return []
        return parse_devices(out)

    def _apply_devices(self, devices: list[dict], chosen: dict | None) -> None:
        self._applying_devices = True
        self.devices = devices
        labels = [self._device_label(item) for item in devices]
        self.device_combo["values"] = labels
        if chosen is None:
            self.device_combo.set("")
        else:
            self.device_combo.current(devices.index(chosen))
        self._applying_devices = False
        if not devices:
            self.status.set("没有检测到设备")
        elif chosen and chosen["state"] not in READY_STATES:
            self.status.set(f"{chosen['serial']}：{STATE_TEXT.get(chosen['state'], chosen['state'])}")
        elif chosen:
            self.status.set(f"已连接 {chosen['serial']}")

    def _device_label(self, device: dict) -> str:
        state = STATE_TEXT.get(device["state"], device["state"])
        model = f"  {device['model']}" if device["model"] else ""
        return f"{device['serial']}{model}  [{state}]"

    def current_device(self) -> dict | None:
        index = self.device_combo.current()
        if index < 0 or index >= len(self.devices):
            return None
        return self.devices[index]

    def require_serial(self, silent: bool = False) -> str | None:
        if not ADB_PATH.is_file():
            if not silent:
                messagebox.showerror("找不到 ADB", f"没有找到：\n{ADB_PATH}", parent=self)
            return None
        device = self.current_device()
        if device is None:
            if not silent:
                messagebox.showwarning("没有设备", "请先点「启动 ADB」，并确认手机已连接。", parent=self)
            return None
        if device["state"] not in READY_STATES:
            if not silent:
                messagebox.showwarning(
                    "设备不可用",
                    f"当前设备状态是「{STATE_TEXT.get(device['state'], device['state'])}」。\n"
                    "未授权时，请在手机上点允许 USB 调试。",
                    parent=self,
                )
            return None
        return device["serial"]

    def on_start_adb(self) -> None:
        remote = norm_remote(self.remote_path.get())
        self.remote_path.set(remote)
        previous = self._selected_serial()
        self.run_job("正在启动 ADB…", lambda: self._start_job(remote, previous))

    def _selected_serial(self) -> str | None:
        device = self.current_device()
        if device is None:
            return None
        return device["serial"]

    def _start_job(self, remote: str, previous: str | None) -> str:
        code = self.stream([self.adb, "start-server"])
        if code != 0:
            self.post("ADB 启动失败")
            return "ADB 启动失败"
        self.post("ADB 已启动")
        return self._refresh_devices_job(remote, list_files=True, previous=previous)

    def on_refresh_devices(self) -> None:
        remote = norm_remote(self.remote_path.get())
        self.remote_path.set(remote)
        previous = self._selected_serial()
        self.run_job(
            "正在刷新设备…",
            lambda: self._refresh_devices_job(remote, list_files=True, previous=previous),
        )

    def _refresh_devices_job(self, remote: str, list_files: bool, previous: str | None) -> str:
        code, out = self.capture([self.adb, "version"], timeout=15)
        if code == 0 and out.strip():
            self.post(out.strip().splitlines()[0])
        devices = self._query_devices()
        chosen = self._pick_device(devices, previous)
        self.ui(lambda: self._apply_devices(devices, chosen))
        if not devices:
            self.post("没有检测到设备。请打开手机 USB 调试后，再点「启动 ADB」。")
            return "没有检测到设备"
        for device in devices:
            model = f" {device['model']}" if device["model"] else ""
            self.post(f"设备 {device['serial']}{model}  {STATE_TEXT.get(device['state'], device['state'])}")
        if list_files and chosen and chosen["state"] in READY_STATES:
            self._list_remote(chosen["serial"], remote)
            return f"已连接 {chosen['serial']}"
        if chosen and chosen["state"] not in READY_STATES:
            return f"{chosen['serial']}：{STATE_TEXT.get(chosen['state'], chosen['state'])}"
        return "就绪"

    def _pick_device(self, devices: list[dict], previous: str | None) -> dict | None:
        usable = [item for item in devices if item["state"] in READY_STATES]
        if previous:
            for item in usable:
                if item["serial"] == previous:
                    return item
        if usable:
            return usable[0]
        return devices[0] if devices else None

    def on_device_changed(self, _event=None) -> None:
        if self._applying_devices or self._busy:
            return
        serial = self.require_serial(silent=True)
        if not serial:
            device = self.current_device()
            if device:
                self.status.set(f"{device['serial']}：{STATE_TEXT.get(device['state'], device['state'])}")
            return
        remote = norm_remote(self.remote_path.get())
        self.remote_path.set(remote)
        self.run_job("正在读取设备目录…", lambda: self._list_remote(serial, remote))

    def on_jump(self, path: str) -> None:
        self.remote_path.set(path)
        self.on_refresh_remote()

    def on_up(self) -> None:
        self.remote_path.set(remote_parent(self.remote_path.get()))
        self.on_refresh_remote()

    def on_refresh_remote(self) -> None:
        serial = self.require_serial()
        if not serial:
            return
        remote = norm_remote(self.remote_path.get())
        self.remote_path.set(remote)
        self.run_job("正在读取设备目录…", lambda: self._list_remote(serial, remote))

    def _list_remote(self, serial: str, path: str) -> str:
        self.post(f"列出 {path}")
        code, out = self.capture(
            [self.adb, "-s", serial, "shell", f"ls -1pa {shell_quote(path)}"],
            timeout=40,
        )
        if code != 0 and re.search(r"invalid option|unknown option|unrecognized", out, re.I):
            code, out = self.capture(
                [self.adb, "-s", serial, "shell", f"ls -1a {shell_quote(path)}"],
                timeout=40,
            )
        error = ""
        entries: list[dict] = []
        if code != 0:
            error = out.strip() or "无法读取目录"
            self.post(error)
            if path == "/data":
                self.post("如果 /data 没有权限，可以改到 /sdcard 或 /data/local/tmp。")
        else:
            for name, is_dir in parse_ls(out):
                entries.append(
                    {
                        "name": name,
                        "is_dir": is_dir,
                        "path": remote_join(path, name),
                    }
                )
            self.post(f"{path}  共 {len(entries)} 项")
        self.ui(lambda items=entries, shown=path, message=error: self._show_remote(items, shown, message))
        if error:
            return f"{path} 读取失败"
        return f"{path}  共 {len(entries)} 项"

    def _show_remote(self, entries: list[dict], path: str, error: str) -> None:
        self.remote_entries = entries
        self.remote_list.delete(0, "end")
        self.remote_path.set(path)
        if error:
            self.remote_hint.set(f"{path}    读取失败")
            self.remote_list.insert("end", "无法读取这个目录")
            for line in error.splitlines()[:4]:
                self.remote_list.insert("end", line)
            return
        self.remote_hint.set(f"{path}    {len(entries)} 项")
        if not entries:
            self.remote_list.insert("end", "（空目录）")
            return
        for entry in entries:
            kind = "目录" if entry["is_dir"] else "文件"
            self.remote_list.insert("end", f"[{kind}]  {entry['name']}")

    def on_open(self) -> None:
        selected = filedialog.askopenfilenames(title="选择要上传到设备的文件", parent=self)
        if not selected:
            return
        added = 0
        for path in selected:
            if path not in self.local_files:
                self.local_files.append(path)
                self.local_list.insert("end", path)
                added += 1
        self.local_hint.set(f"待上传  {len(self.local_files)} 个文件")
        self.status.set(f"已加入 {added} 个文件")

    def on_remove_local(self) -> None:
        indexes = list(self.local_list.curselection())
        if not indexes:
            return
        for index in reversed(indexes):
            if 0 <= index < len(self.local_files):
                self.local_files.pop(index)
            self.local_list.delete(index)
        self.local_hint.set(f"待上传  {len(self.local_files)} 个文件")

    def on_clear_local(self) -> None:
        self.local_files.clear()
        self.local_list.delete(0, "end")
        self.local_hint.set("待上传  0 个文件")

    def on_upload(self) -> None:
        serial = self.require_serial()
        if not serial:
            return
        if not self.local_files:
            messagebox.showwarning("没有文件", "请先点「打开」选择要上传的文件。", parent=self)
            return
        remote = norm_remote(self.remote_path.get())
        self.remote_path.set(remote)
        files = list(self.local_files)
        self.run_job("正在上传…", lambda: self._upload_job(serial, files, remote))

    def _upload_job(self, serial: str, files: list[str], remote: str) -> str:
        ok = 0
        failed = 0
        for local in files:
            target = remote_join(remote, Path(local).name)
            self.post(f"上传 {local}  →  {target}")
            code = self.stream([self.adb, "-s", serial, "push", local, target])
            if code == 0:
                ok += 1
            else:
                failed += 1
        self.post(f"上传结束：成功 {ok} 个，失败 {failed} 个")
        self._list_remote(serial, remote)
        return f"上传完成：成功 {ok} 个，失败 {failed} 个"

    def selected_remote_entries(self) -> list[dict]:
        chosen: list[dict] = []
        for index in self.remote_list.curselection():
            if 0 <= index < len(self.remote_entries):
                chosen.append(self.remote_entries[index])
        return chosen

    def on_enter(self) -> None:
        chosen = self.selected_remote_entries()
        if len(chosen) != 1 or not chosen[0]["is_dir"]:
            messagebox.showinfo("进入目录", "请在右侧选中一个目录。", parent=self)
            return
        self.remote_path.set(chosen[0]["path"])
        self.on_refresh_remote()

    def on_delete_remote(self) -> None:
        serial = self.require_serial()
        if not serial:
            return
        chosen = self.selected_remote_entries()
        if not chosen:
            messagebox.showwarning("没有选择文件", "请先在右侧选中要删除的文件或目录。", parent=self)
            return
        names = [entry["name"] for entry in chosen]
        if len(names) == 1:
            prompt = f"确定要删除「{names[0]}」吗？"
        else:
            prompt = f"确定要删除以下 {len(names)} 项吗？\n" + "\n".join(names[:10])
            if len(names) > 10:
                prompt += f"\n…等共 {len(names)} 项"
        if not messagebox.askyesno("确认删除", prompt, parent=self, icon="warning"):
            return
        items = list(chosen)
        self.run_job("正在删除…", lambda: self._delete_remote_job(serial, items))

    def _delete_remote_job(self, serial: str, entries: list[dict]) -> str:
        ok = 0
        failed = 0
        for entry in entries:
            path = entry["path"]
            cmd = "rm -r" if entry["is_dir"] else "rm"
            self.post(f"删除 {path}")
            code, out = self.capture(
                [self.adb, "-s", serial, "shell", f"{cmd} {shell_quote(path)}"],
                timeout=40,
            )
            if code == 0:
                ok += 1
            else:
                failed += 1
                self.post(out.strip() or "删除失败")
        self.post(f"删除结束：成功 {ok} 个，失败 {failed} 个")
        remote = norm_remote(self.remote_path.get())
        self._list_remote(serial, remote)
        return f"删除完成：成功 {ok} 个，失败 {failed} 个"

    def on_remote_activate(self, _event=None) -> None:
        chosen = self.selected_remote_entries()
        if len(chosen) != 1:
            return
        if chosen[0]["is_dir"]:
            self.remote_path.set(chosen[0]["path"])
            self.on_refresh_remote()
        else:
            self.on_download()

    def on_download(self) -> None:
        serial = self.require_serial()
        if not serial:
            return
        chosen = self.selected_remote_entries()
        if not chosen:
            messagebox.showwarning("没有选择文件", "请先在右侧选中要下载的文件或目录。", parent=self)
            return
        initial_dir = self._last_download
        if not Path(initial_dir).is_dir():
            initial_dir = self._default_download_dir()
        dest = filedialog.askdirectory(
            title="选择下载到电脑的文件夹",
            parent=self,
            initialdir=initial_dir,
            mustexist=True,
        )
        if not dest:
            return
        self._last_download = dest
        self.settings["last_download_dir"] = dest
        save_settings(self.settings)
        items = list(chosen)
        self.run_job("正在下载…", lambda: self._download_job(serial, items, dest))

    def _download_job(self, serial: str, entries: list[dict], dest: str) -> str:
        ok = 0
        failed = 0
        for entry in entries:
            self.post(f"下载 {entry['path']}  →  {dest}")
            code = self.stream([self.adb, "-s", serial, "pull", entry["path"], dest])
            if code == 0:
                ok += 1
            else:
                failed += 1
        self.post(f"下载结束：成功 {ok} 个，失败 {failed} 个。保存位置：{dest}")
        return f"下载完成：成功 {ok} 个，失败 {failed} 个"


def self_test() -> None:
    assert shell_quote("/data") == "'/data'"
    assert shell_quote("/data/a'b") == "'/data/a'\\''b'"
    assert norm_remote("") == "/data"
    assert norm_remote("data") == "/data"
    assert norm_remote(r"\data\local") == "/data/local"
    assert norm_remote("/data/../sdcard") == "/sdcard"
    assert remote_join("/data", "a.txt") == "/data/a.txt"
    assert remote_join("/", "a.txt") == "/a.txt"
    assert remote_parent("/data") == "/"
    assert remote_parent("/data/local/tmp") == "/data/local"
    assert remote_parent("/") == "/"

    devices = parse_devices(
        "List of devices attached\n"
        "* daemon started successfully\n"
        "emulator-5554          device product:sdk model:sdk_gphone device:generic\n"
        "ABC123               unauthorized\n"
    )
    assert [item["serial"] for item in devices] == ["emulator-5554", "ABC123"]
    assert devices[0]["model"] == "sdk_gphone"
    assert devices[1]["state"] == "unauthorized"

    listed = parse_ls(".\n..\n./\n../\nfile.txt\nsubdir/\n")
    assert ("subdir", True) in listed
    assert ("file.txt", False) in listed
    assert all(name not in {".", ".."} for name, _is_dir in listed)
    colored = parse_ls("\x1b[1;34m.\x1b[m/\n\x1b[0;0m22.zip\x1b[m\n\x1b[1;34mapplog\x1b[m/\n")
    assert ("22.zip", False) in colored
    assert ("applog", True) in colored
    assert all("\x1b" not in name for name, _is_dir in colored)

    enable_dpi()
    app = AdbGui(autostart=False)
    app.update()
    assert app.btn_start.cget("text") == "启动 ADB"
    assert app.btn_open.cget("text") == "打开"
    assert app.btn_upload.cget("text") == "上传"
    assert app.btn_download.cget("text") == "下载"
    assert app.btn_delete_remote.cget("text") == "删除"
    assert app.remote_path.get() == "/data"
    code, out = app.capture([app.adb, "version"], timeout=15)
    app.update()
    app._poll()
    assert code == 0, out
    assert "Android Debug Bridge" in out
    print(out.strip())
    _device_code, device_out = app.capture([app.adb, "devices", "-l"], timeout=20)
    app._poll()
    print(device_out.strip())
    ready = [item for item in parse_devices(device_out) if item["state"] in READY_STATES]
    if ready:
        serial = ready[0]["serial"]
        list_code, listing = app.capture(
            [app.adb, "-s", serial, "shell", "ls -1pa '/data'"],
            timeout=40,
        )
        app._poll()
        assert list_code == 0, listing
        remote_entries = parse_ls(listing)
        assert remote_entries, listing
        assert all("\x1b" not in name for name, _is_dir in remote_entries)
        print(f"REMOTE {serial} /data {len(remote_entries)} entries")
        for name, is_dir in remote_entries[:12]:
            print(("D " if is_dir else "F ") + name)
    app._on_close()
    print("SELF_TEST_OK")


def main() -> None:
    enable_dpi()
    app = AdbGui(autostart=True)
    app.mainloop()


if __name__ == "__main__":
    try:
        if "--self-test" in sys.argv:
            self_test()
        else:
            main()
    except Exception:
        write_error(traceback.format_exc())
        raise
