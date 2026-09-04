import asyncio
import ctypes
import io
import json
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox

import qrcode
from aiohttp import WSMsgType, web
from PIL import Image, ImageTk


APP_NAME = "跨屏输入"
PORT = 8765
WEB_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "web"
ICON_PATH = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "app_icon.ico"


def local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", INPUT_UNION)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D
VK_BACK = 0x08
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_V = 0x56
CF_DIB = 8
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002


def _send_key(scan: int, flags: int = 0, virtual_key: int = 0) -> None:
    entry = INPUT(
        type=INPUT_KEYBOARD,
        union=INPUT_UNION(
            ki=KEYBDINPUT(
                wVk=virtual_key,
                wScan=scan,
                dwFlags=flags,
                time=0,
                dwExtraInfo=0,
            )
        ),
    )
    sent = ctypes.windll.user32.SendInput(1, ctypes.byref(entry), ctypes.sizeof(INPUT))
    if sent != 1:
        raise ctypes.WinError()


def type_text(value: str) -> None:
    # Phone newlines map to Shift+Enter so chat apps do not submit accidentally.
    for character in value.replace("\r\n", "\n").replace("\r", "\n"):
        if character == "\n":
            press_shift_enter()
            continue
        units = character.encode("utf-16-le")
        for index in range(0, len(units), 2):
            unit = int.from_bytes(units[index:index + 2], "little")
            _send_key(unit, KEYEVENTF_UNICODE)
            _send_key(unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)


def press_enter() -> None:
    _send_key(0, virtual_key=VK_RETURN)
    _send_key(0, KEYEVENTF_KEYUP, virtual_key=VK_RETURN)


def press_shift_enter() -> None:
    _send_key(0, virtual_key=VK_SHIFT)
    press_enter()
    _send_key(0, KEYEVENTF_KEYUP, virtual_key=VK_SHIFT)


def press_ctrl_enter() -> None:
    _send_key(0, virtual_key=VK_CONTROL)
    press_enter()
    _send_key(0, KEYEVENTF_KEYUP, virtual_key=VK_CONTROL)


def press_backspace(count: int) -> None:
    for _ in range(max(0, min(count, 10000))):
        _send_key(0, virtual_key=VK_BACK)
        _send_key(0, KEYEVENTF_KEYUP, virtual_key=VK_BACK)


def press_paste() -> None:
    _send_key(0, virtual_key=VK_CONTROL)
    _send_key(0, virtual_key=VK_V)
    _send_key(0, KEYEVENTF_KEYUP, virtual_key=VK_V)
    _send_key(0, KEYEVENTF_KEYUP, virtual_key=VK_CONTROL)


def set_clipboard_bytes(format_id: int, data: bytes) -> None:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    if not user32.OpenClipboard(None):
        raise ctypes.WinError()
    handle = None
    try:
        if not user32.EmptyClipboard():
            raise ctypes.WinError()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise ctypes.WinError()
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise ctypes.WinError()
        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(format_id, handle):
            raise ctypes.WinError()
        handle = None  # The clipboard owns the allocation after SetClipboardData.
    finally:
        if handle:
            kernel32.GlobalFree(handle)
        user32.CloseClipboard()


class DROPFILES(ctypes.Structure):
    _fields_ = [
        ("pFiles", ctypes.c_ulong),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
        ("fNC", ctypes.c_int),
        ("fWide", ctypes.c_int),
    ]


def paste_image(path: Path) -> None:
    with Image.open(path) as image:
        output = io.BytesIO()
        image.convert("RGB").save(output, "BMP")
    set_clipboard_bytes(CF_DIB, output.getvalue()[14:])
    press_paste()


def paste_file(path: Path) -> None:
    header = DROPFILES(pFiles=ctypes.sizeof(DROPFILES), fWide=1)
    paths = (str(path) + "\0\0").encode("utf-16-le")
    set_clipboard_bytes(CF_HDROP, bytes(header) + paths)
    press_paste()


@dataclass
class UiEvent:
    kind: str
    detail: str = ""


class InputServer:
    def __init__(self, token: str, on_event):
        self.token = token
        self.on_event = on_event
        self.runner = None
        self.loop = None
        self.thread = None
        self.clients = set()
        self.last_sequences = {}
        self.upload_dir = Path(tempfile.gettempdir()) / "LanVoiceInput" / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    async def index(self, request):
        return web.FileResponse(WEB_DIR / "index.html")

    async def manifest(self, request):
        return web.FileResponse(WEB_DIR / "manifest.webmanifest")

    async def upload(self, request):
        if request.query.get("token") != self.token:
            raise web.HTTPForbidden(text="配对码无效")
        kind = request.query.get("kind")
        if kind not in {"image", "file"}:
            raise web.HTTPBadRequest(text="上传类型无效")

        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.name != "upload":
            raise web.HTTPBadRequest(text="没有收到文件")

        original_name = Path(part.filename or "upload.bin").name
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", original_name).strip(" .")
        if not safe_name:
            safe_name = "upload.bin"
        destination = self.upload_dir / f"{secrets.token_hex(5)}-{safe_name}"
        size = 0
        with destination.open("wb") as output:
            while chunk := await part.read_chunk(256 * 1024):
                size += len(chunk)
                output.write(chunk)

        try:
            if kind == "image":
                await asyncio.to_thread(paste_image, destination)
            else:
                await asyncio.to_thread(paste_file, destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise

        self.on_event(UiEvent("uploaded", safe_name))
        return web.json_response({"ok": True, "name": safe_name, "size": size})

    async def websocket(self, request):
        if request.query.get("token") != self.token:
            raise web.HTTPForbidden(text="配对码无效")
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.clients.add(ws)
        self.on_event(UiEvent("connected", request.remote or "手机"))
        await ws.send_json({"type": "ready"})
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(message.data)
                    sequence = payload.get("sequence")
                    client_id = str(payload.get("clientId", ""))
                    last_sequence = self.last_sequences.get(client_id, 0)
                    if client_id and isinstance(sequence, int) and sequence <= last_sequence:
                        await ws.send_json({"type": "ack", "sequence": sequence})
                        continue

                    if payload.get("type") == "sync":
                        remove = int(payload.get("remove", 0))
                        value = str(payload.get("value", ""))
                        if remove:
                            press_backspace(remove)
                        if value:
                            type_text(value)
                        self.on_event(UiEvent("typed", value))
                    elif payload.get("type") == "newline":
                        press_shift_enter()
                        self.on_event(UiEvent("newline"))
                    elif payload.get("type") == "adjust":
                        press_ctrl_enter()
                        self.on_event(UiEvent("adjust"))
                    elif payload.get("type") == "send":
                        press_enter()
                        self.on_event(UiEvent("send"))
                    if client_id and isinstance(sequence, int):
                        self.last_sequences[client_id] = sequence
                    await ws.send_json({"type": "ack", "sequence": sequence})
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": str(exc)})
                    self.on_event(UiEvent("error", str(exc)))
        finally:
            self.clients.discard(ws)
            self.on_event(UiEvent("disconnected"))
        return ws

    async def start_async(self):
        app = web.Application(client_max_size=100 * 1024 * 1024)
        app.router.add_get("/", self.index)
        app.router.add_get("/ws", self.websocket)
        app.router.add_post("/upload", self.upload)
        app.router.add_get("/manifest.webmanifest", self.manifest)
        app.router.add_static("/static", WEB_DIR)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", PORT)
        await site.start()
        self.on_event(UiEvent("server_ready"))

    def start(self):
        def worker():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(self.start_async())
                self.loop.run_forever()
            except OSError as exc:
                self.on_event(UiEvent("server_error", str(exc)))

        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()

    def stop(self):
        if not self.loop or not self.runner:
            return

        async def cleanup():
            for client in tuple(self.clients):
                await client.close()
            await self.runner.cleanup()
            self.loop.stop()

        asyncio.run_coroutine_threadsafe(cleanup(), self.loop)


class DesktopApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        if ICON_PATH.exists():
            self.root.iconbitmap(default=str(ICON_PATH))
        self.root.geometry("460x650")
        self.root.minsize(420, 610)
        self.root.configure(bg="#f4f6f8")
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.token = secrets.token_urlsafe(12)
        self.ip = local_ip()
        self.url = f"http://{self.ip}:{PORT}/?token={self.token}"
        self.server = InputServer(self.token, self.handle_event)
        self.qr_image = None
        self.build_ui()
        self.server.start()

    def build_ui(self):
        header = tk.Frame(self.root, bg="#17324d", height=104)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header, text=APP_NAME, font=("Microsoft YaHei UI", 20, "bold"),
            fg="white", bg="#17324d"
        ).pack(anchor="w", padx=28, pady=(22, 2))
        tk.Label(
            header, text="Android 手机输入，Windows 光标接收",
            font=("Microsoft YaHei UI", 10), fg="#c8d9e8", bg="#17324d"
        ).pack(anchor="w", padx=29)

        content = tk.Frame(self.root, bg="#f4f6f8")
        content.pack(fill="both", expand=True, padx=28, pady=22)

        self.status_dot = tk.Label(content, text="●", font=("Arial", 14), fg="#d99614", bg="#f4f6f8")
        self.status_dot.pack()
        self.status_label = tk.Label(
            content, text="正在启动…", font=("Microsoft YaHei UI", 12, "bold"),
            fg="#253746", bg="#f4f6f8"
        )
        self.status_label.pack(pady=(0, 10))

        qr = qrcode.QRCode(version=None, box_size=7, border=2)
        qr.add_data(self.url)
        qr.make(fit=True)
        image = qr.make_image(fill_color="#172b3a", back_color="white").convert("RGB")
        self.qr_image = ImageTk.PhotoImage(image)
        tk.Label(content, image=self.qr_image, bg="white", bd=0).pack(pady=4)

        tk.Label(
            content, text="用手机相机扫描二维码连接", font=("Microsoft YaHei UI", 11),
            fg="#253746", bg="#f4f6f8"
        ).pack(pady=(12, 3))
        tk.Label(
            content, text=f"手机和电脑需连接同一 Wi-Fi\n{self.ip}:{PORT}",
            font=("Microsoft YaHei UI", 9), fg="#667784", bg="#f4f6f8", justify="center"
        ).pack()

        self.activity_label = tk.Label(
            content, text="连接后，请把电脑光标放到需要输入的位置",
            font=("Microsoft YaHei UI", 9), fg="#667784", bg="#f4f6f8",
            wraplength=360, justify="center"
        )
        self.activity_label.pack(side="bottom", pady=(10, 0))

    def handle_event(self, event: UiEvent):
        self.root.after(0, lambda: self.apply_event(event))

    def apply_event(self, event: UiEvent):
        if event.kind == "server_ready":
            self.status_dot.config(fg="#d99614")
            self.status_label.config(text="等待手机连接")
        elif event.kind == "connected":
            self.status_dot.config(fg="#16866b")
            self.status_label.config(text="手机已连接")
            self.activity_label.config(text="连接成功，现在把电脑光标放到需要输入的位置")
        elif event.kind == "disconnected":
            self.status_dot.config(fg="#d99614")
            self.status_label.config(text="手机已断开，等待重连")
        elif event.kind == "typed":
            count = len(event.detail)
            self.activity_label.config(text=f"刚刚输入了 {count} 个字符")
        elif event.kind == "newline":
            self.activity_label.config(text="已换行")
        elif event.kind == "adjust":
            self.activity_label.config(text="已发送调整方向")
        elif event.kind == "send":
            self.activity_label.config(text="已发送")
        elif event.kind == "uploaded":
            self.activity_label.config(text=f"已粘贴附件：{event.detail}")
        elif event.kind == "error":
            self.activity_label.config(text=f"输入失败：{event.detail}")
        elif event.kind == "server_error":
            self.status_dot.config(fg="#c24c42")
            self.status_label.config(text="启动失败")
            self.activity_label.config(text=f"端口 {PORT} 被占用，请关闭其他实例后重试")

    def close(self):
        self.server.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    try:
        DesktopApp().run()
    except OSError as exc:
        messagebox.showerror(APP_NAME, f"启动失败：{exc}\n\n请确认端口 {PORT} 没有被占用。")
