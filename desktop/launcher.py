#!/usr/bin/python3
"""Launch the local receipt service and its Linux GTK/WebKit application.

This file deliberately uses only Python's standard library. The native window
uses the system Python; the backend uses the app's bundled Python runtime.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


APP_ROOT = Path(__file__).resolve().parent.parent
APP_ID = "receipt-archive"
SYSTEM_PYTHON = Path("/usr/bin/python3")
DEFAULT_URL = "http://127.0.0.1:8765"
STARTUP_TIMEOUT = 90.0
_LOCAL_URL = re.compile(r"http://127\.0\.0\.1:([1-9]\d{0,4})")
MAX_LOCAL_JSON_BYTES = 32 * 1024 * 1024


class LaunchError(RuntimeError):
    """An actionable application startup error, safe to display to the user."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # A foreign service on the preferred port must never redirect a local
        # health request to an external website.
        return None


def clean_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    value = dict(os.environ if environment is None else environment)
    # In particular, don't load Miniconda's C libraries into system GTK/WebKit.
    for key in ("PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH", "CONDA_PREFIX",
                "CONDA_DEFAULT_ENV", "VIRTUAL_ENV", "GI_TYPELIB_PATH",
                "GIO_MODULE_DIR", "GSETTINGS_SCHEMA_DIR"):
        value.pop(key, None)
    return value


def _xdg_base(environment: dict[str, str], variable: str, suffix: str) -> Path:
    configured = environment.get(variable)
    if configured and Path(configured).is_absolute():
        return Path(configured)
    return Path(environment.get("HOME") or str(Path.home())) / suffix


def state_directory(environment: dict[str, str]) -> Path:
    return _xdg_base(environment, "XDG_STATE_HOME", ".local/state") / APP_ID


def data_directory(environment: dict[str, str]) -> Path:
    configured = environment.get("RECEIPT_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return _xdg_base(environment, "XDG_DATA_HOME", ".local/share") / APP_ID / "data"


def backend_python(root: Path) -> Path | None:
    for candidate in (root / "runtime/bin/python", root / ".venv/bin/python"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def valid_service_url(value: object) -> bool:
    match = _LOCAL_URL.fullmatch(value) if isinstance(value, str) else None
    return bool(match and int(match.group(1)) <= 65535)


def fetch_json(url: str, timeout: float = 1.0):
    """Read small local JSON responses, without consulting external proxies."""
    opener = build_opener(ProxyHandler({}), _NoRedirects())
    try:
        with opener.open(url, timeout=timeout) as response:
            if response.status != 200:
                return None
            raw = response.read(MAX_LOCAL_JSON_BYTES + 1)
            if len(raw) > MAX_LOCAL_JSON_BYTES:
                return None
            return json.loads(raw)
    except (URLError, OSError, ValueError, TimeoutError, HTTPException):
        return None


def healthy_service(url: str) -> bool:
    if not valid_service_url(url):
        return False
    value = fetch_json(url + "/api/health")
    if not isinstance(value, dict) or value.get("status") != "ok" or value.get("local_only") is not True:
        return False
    if "app_id" in value:
        return value["app_id"] == APP_ID
    # Recognise earlier releases already serving the user's current batch.
    return value.get("max_files") == 500 and value.get("max_file_mb") == 20


def saved_service_url(environment: dict[str, str]) -> str | None:
    try:
        saved = json.loads((state_directory(environment) / "service.json").read_text("utf-8"))
        value = saved.get("url") if isinstance(saved, dict) else None
        return value if valid_service_url(value) else None
    except (OSError, ValueError):
        return None


def find_service(environment: dict[str, str]) -> str | None:
    if healthy_service(DEFAULT_URL):
        return DEFAULT_URL
    saved = saved_service_url(environment)
    if saved and saved != DEFAULT_URL and healthy_service(saved):
        return saved
    return None


def select_port(preferred: int = 8765) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", preferred))
        except OSError:
            listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def _startup_lock(directory: Path):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _remember_service(directory: Path, url: str, pid: int):
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         prefix="service-", suffix=".tmp", delete=False) as output:
            temporary = output.name
            json.dump({"url": url, "pid": pid}, output)
        Path(temporary).replace(directory / "service.json")
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def _wait_for_service(url: str, process, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        if healthy_service(url):
            return
        code = process.poll()
        if code is not None:
            raise LaunchError(f"本地服务启动失败（退出码 {code}），请查看应用状态目录中的 backend.log。")
        if time.monotonic() >= deadline:
            # This is our newly spawned, unready child, never an existing service.
            process.terminate()
            raise LaunchError("本地服务启动超时，请查看应用状态目录中的 backend.log 后重新打开。")
        time.sleep(0.2)


def ensure_backend(root: Path = APP_ROOT, environment: dict[str, str] | None = None,
                   timeout: float = STARTUP_TIMEOUT) -> str:
    environment = dict(os.environ if environment is None else environment)
    existing = find_service(environment)
    if existing:
        return existing
    state = state_directory(environment)
    with _startup_lock(state):
        # Another launch may have finished while this process waited for the lock.
        existing = find_service(environment)
        if existing:
            return existing
        python = backend_python(root)
        if python is None:
            raise LaunchError("应用运行环境不完整：找不到 runtime/bin/python 或 .venv/bin/python，请重新安装应用。")
        directory = data_directory(environment)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        child_environment = clean_environment(environment)
        child_environment["RECEIPT_DATA_DIR"] = str(directory)
        child_environment["PYTHONUNBUFFERED"] = "1"
        for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
                         "NUMEXPR_NUM_THREADS", "RECEIPT_OCR_THREADS"):
            child_environment[variable] = "2"
        port = select_port()
        url = f"http://127.0.0.1:{port}"
        command = [str(python), "-m", "receipt_tool.app", "--host", "127.0.0.1", "--port", str(port)]
        try:
            with (state / "backend.log").open("ab", buffering=0) as log:
                process = subprocess.Popen(command, cwd=root, env=child_environment,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True)
        except OSError as exc:
            raise LaunchError("无法启动本地服务，请检查应用运行环境和状态目录权限。") from exc
        _wait_for_service(url, process, timeout)
        _remember_service(state, url, process.pid)
        return url


def latest_batch_id(url: str) -> str | None:
    value = fetch_json(url + "/api/batches/latest")
    if isinstance(value, dict) and "batch" in value:
        value = value["batch"]
    ident = value.get("id") if isinstance(value, dict) else None
    return ident if isinstance(ident, str) and re.fullmatch(r"[a-f0-9]{32}", ident) else None


def native_dependencies(environment: dict[str, str]) -> dict:
    code = """import gi, json, sys
gi.require_version('Gtk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
try:
    gi.require_version('WebKit2', '4.1')
    api = '4.1'
except ValueError:
    gi.require_version('WebKit2', '4.0')
    api = '4.0'
from gi.repository import GdkPixbuf, Gtk, WebKit2
version = (WebKit2.get_major_version(), WebKit2.get_minor_version(), WebKit2.get_micro_version())
svg = any(fmt.get_name() == 'svg' and not fmt.is_disabled() for fmt in GdkPixbuf.Pixbuf.get_formats())
print(json.dumps({'available': sys.version_info >= (3, 8) and version >= (2, 36, 0) and svg,
                  'webkit_api': api, 'webkit_version': list(version), 'svg_loader': svg}))
"""
    dependencies = {"available": False,
                    "required_packages": ["python3-gi", "gir1.2-gtk-3.0", "librsvg2-common"],
                    "webkit_alternatives": ["gir1.2-webkit2-4.1", "gir1.2-webkit2-4.0"],
                    "minimum_webkit_version": "2.36"}
    child_environment = clean_environment(environment)
    child_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run([str(SYSTEM_PYTHON), "-c", code],
            env=child_environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False, text=True)
        if result.returncode == 0:
            dependencies.update(json.loads(result.stdout))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return dependencies


def show_error(message: str, environment: dict[str, str]):
    print(f"凭证归档：{message}", file=sys.stderr)
    if not (environment.get("DISPLAY") or environment.get("WAYLAND_DISPLAY")):
        return
    zenity = shutil.which("zenity", path=environment.get("PATH"))
    if zenity:
        try:
            subprocess.Popen([zenity, "--error", "--title=凭证归档", "--no-wrap", f"--text={message}"],
                env=clean_environment(environment), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True)
        except OSError:
            pass


def run_native_gui(root: Path, url: str, batch_id: str | None, environment: dict[str, str]) -> int:
    shell = root / "desktop/receipt_desktop.py"
    if not shell.is_file():
        raise LaunchError("应用窗口组件缺失，请重新安装应用。")
    command = [str(SYSTEM_PYTHON), str(shell), "--url", url]
    if batch_id:
        command.extend(["--batch-id", batch_id])
    try:
        result = subprocess.run(command, cwd=root, env=clean_environment(environment), check=False)
    except OSError as exc:
        raise LaunchError("无法打开应用窗口，请检查 Linux 图形组件。") from exc
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动凭证归档 Linux 应用")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只检查依赖和现有服务，不创建文件或启动服务")
    mode.add_argument("--no-window", action="store_true", help="仅启动或复用本地服务，用于本机检查")
    args = parser.parse_args(argv)
    environment = dict(os.environ)
    if args.check:
        service = find_service(environment)
        python = backend_python(APP_ROOT)
        dependencies = native_dependencies(environment)
        ready = bool(dependencies["available"] and (service or python)
                     and (APP_ROOT / "desktop/receipt_desktop.py").is_file())
        print(json.dumps({"ready": ready, "backend_python": str(python) if python else None,
                          "native_python": str(SYSTEM_PYTHON), "native_dependencies": dependencies,
                          "service_url": service, "service_healthy": service is not None}, ensure_ascii=False))
        return 0 if ready else 1
    try:
        if not args.no_window and not native_dependencies(environment)["available"]:
            raise LaunchError("缺少 Linux 图形组件。请安装 python3-gi、gir1.2-gtk-3.0、"
                              "librsvg2-common，以及 gir1.2-webkit2-4.1 或 gir1.2-webkit2-4.0，"
                              "并将 WebKitGTK 更新至 2.36 或更新版本后重新打开应用。")
        service = ensure_backend(APP_ROOT, environment)
        if args.no_window:
            print(service)
            return 0
        result = run_native_gui(APP_ROOT, service, latest_batch_id(service), environment)
        if result:
            raise LaunchError("应用窗口启动失败，请检查 Linux 图形组件后重新打开。")
        return 0
    except (LaunchError, OSError) as exc:
        show_error(str(exc), environment)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
