"""Windows desktop entry using the bundled CPython and Edge App Mode / WebView2."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
from http.client import HTTPException
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
import webbrowser


ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "凭证归档"
APP_ID = "receipt-archive"
WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"
DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "ReceiptArchive"


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def fetch_json(url):
    try:
        with build_opener(ProxyHandler({}), _NoRedirects()).open(url, timeout=1) as response:
            if response.status != 200:
                return None
            raw = response.read(32 * 1024 * 1024 + 1)
            return json.loads(raw) if len(raw) <= 32 * 1024 * 1024 else None
    except (OSError, URLError, ValueError, HTTPException):
        return None


def healthy(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not 0 < parsed.port <= 65535:
            return False
    except (ValueError, TypeError):
        return False
    body = fetch_json(url + "/api/health")
    return isinstance(body, dict) and body.get("app_id") == APP_ID and body.get("status") == "ok"


def service_url():
    candidates = ["http://127.0.0.1:8765"]
    try:
        candidates.append(json.loads((DATA_ROOT / "service.json").read_text("utf-8"))["url"])
    except (OSError, ValueError, KeyError):
        pass
    return next((url for url in candidates if isinstance(url, str) and healthy(url)), None)


def _start_backend_locked():
    existing = service_url()
    if existing:
        return existing
    runtime = ROOT / "runtime" / "python.exe"
    if not runtime.is_file():
        raise RuntimeError("运行文件不完整，请完整解压 Windows ZIP 后再打开。")
    environment = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(key, None)
    environment["RECEIPT_DATA_DIR"] = str(Path(environment.get("RECEIPT_DATA_DIR", str(DATA_ROOT / "data"))))
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "RECEIPT_OCR_THREADS"):
        environment[variable] = "2"
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", 8765))
        except OSError:
            listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    with (DATA_ROOT / "backend.log").open("ab", buffering=0) as log:
        process = subprocess.Popen(
            [str(runtime), "-m", "receipt_tool.app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if healthy(url):
            with tempfile.NamedTemporaryFile("w", dir=DATA_ROOT, encoding="utf-8", delete=False) as output:
                json.dump({"url": url, "pid": process.pid}, output)
                temporary = Path(output.name)
            temporary.replace(DATA_ROOT / "service.json")
            return url
        if process.poll() is not None:
            raise RuntimeError("本地服务未能启动，请查看应用数据目录中的 backend.log。")
        time.sleep(0.2)
    process.terminate()
    raise RuntimeError("本地服务启动超时，请重新打开应用并检查 backend.log。")


def start_backend():
    import msvcrt

    with (DATA_ROOT / "startup.lock").open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        deadline = time.monotonic() + 95
        while True:
            try:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("另一个启动任务尚未完成，请稍后重新打开。")
                time.sleep(0.2)
        try:
            return _start_backend_locked()
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def single_window():
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    key = hashlib.sha256(str(DATA_ROOT).encode()).hexdigest()[:20]
    mutex = kernel.CreateMutexW(None, False, "Local\\ReceiptArchiveDesktop-" + key)
    if not mutex:
        raise OSError("无法创建应用启动锁。")
    if ctypes.get_last_error() == 183:
        user = ctypes.WinDLL("user32")
        user.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
        user.FindWindowW.restype = wintypes.HWND
        user.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user.ShowWindow.restype = wintypes.BOOL
        user.SetForegroundWindow.argtypes = (wintypes.HWND,)
        user.SetForegroundWindow.restype = wintypes.BOOL
        window = user.FindWindowW(None, APP_NAME)
        if window:
            user.ShowWindow(window, 9)
            user.SetForegroundWindow(window)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle(mutex)
        return None
    return mutex


def _find_edge_executable() -> Path | None:
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for path in candidates:
        if path.is_file():
            return path
    which_edge = shutil.which("msedge") or shutil.which("msedge.exe")
    return Path(which_edge) if which_edge else None


def native_window(url: str) -> None:
    version = (ROOT / "VERSION").read_text("utf-8").strip()
    target_url = f"{url}/?desktop={version}"
    try:
        import webview  # type: ignore[import-not-found]

        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["ALLOW_FILE_URLS"] = False
        window = webview.create_window(
            APP_NAME,
            target_url,
            width=1180,
            height=850,
            min_size=(660, 540),
            text_select=True,
        )
        latest = fetch_json(url + "/api/batches/latest")
        batch = latest.get("id") if isinstance(latest, dict) else None
        if isinstance(latest, dict) and isinstance(latest.get("batch"), dict):
            batch = latest["batch"].get("id")
        if isinstance(batch, str) and len(batch) == 32 and all(char in "0123456789abcdef" for char in batch):
            def restore_batch():
                window.evaluate_js(
                    """(() => { const key='receipt-studio:last-batch';
                    if(!localStorage.getItem(key)){localStorage.setItem(key,%s);location.reload();}
                })()"""
                    % json.dumps(batch)
                )

            window.events.loaded += restore_batch
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(DATA_ROOT / "webview"), debug=False)
        return
    except ImportError:
        pass

    edge = _find_edge_executable()
    if edge is not None:
        profile_dir = DATA_ROOT / "webview"
        profile_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                str(edge),
                f"--app={target_url}",
                "--window-size=1180,850",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            check=False,
        )
    else:
        webbrowser.open(target_url)


def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--browser", action="store_true", help="使用默认浏览器打开本机工作区")
    args = parser.parse_args()
    if os.name != "nt":
        print("此入口用于 Windows 10/11 x64；Linux 请使用 desktop.sh。", file=sys.stderr)
        return 1
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=DATA_ROOT / "desktop.log", encoding="utf-8", level=logging.INFO)
    mutex = None
    try:
        if not args.browser:
            mutex = single_window()
            if mutex is None:
                return 0
        url = start_backend()
        if args.browser:
            webbrowser.open(url)
        else:
            native_window(url)
        return 0
    except Exception:
        detail = traceback.format_exc()
        logging.error(detail)
        if sys.stderr is not None:
            print(detail, file=sys.stderr)
        message = (
            "应用未能打开。请确认已完整解压压缩包。\n\n"
            "独立桌面窗口需要 Microsoft Edge / WebView2 Runtime：\n"
            + WEBVIEW2_URL
            + "\n\nOCR 需要 Microsoft Visual C++ v14 x64 Runtime：\n"
            "https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist"
            "\n\n也可以双击 run-browser.cmd 使用浏览器方式。"
            "\n详细信息保存在："
            + str(DATA_ROOT / "desktop.log")
        )
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
        return 1
    finally:
        if mutex:
            kernel = ctypes.WinDLL("kernel32")
            kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
