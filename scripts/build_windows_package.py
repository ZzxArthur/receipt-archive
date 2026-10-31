#!/usr/bin/env python3
"""Assemble lightweight Windows x64 CPython + slim wheels into a portable application ZIP.

Uses a lean HTTP/document stack and RapidOCRLite native CPU inference.
"""
from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.13.17"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_SHA256 = "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"
REQUIRED_PACKAGES = {"rapidocrlite", "numpy", "pillow", "pyyaml", "requests", "tqdm"}


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def download(url: str, path: Path, expected: str | None = None):
    if path.is_file() and (expected is None or digest(path) == expected):
        return
    temporary = path.with_suffix(path.suffix + ".downloading")
    for attempt in range(3):
        try:
            with urlopen(url, timeout=45) as source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target)
            break
        except OSError:
            temporary.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    if expected and digest(temporary) != expected:
        temporary.unlink()
        raise RuntimeError("下载文件的 SHA-256 与官方发布信息不一致：" + path.name)
    temporary.replace(path)


def runtime_inventory(site: Path) -> list[dict[str, str]]:
    inventory = []
    for metadata in sorted(site.glob("*.dist-info/METADATA")):
        parsed = Parser().parsestr(metadata.read_text("utf-8"))
        inventory.append({"name": parsed["Name"], "version": parsed["Version"]})
    names = {re.sub(r"[-_.]+", "-", item["name"].lower()) for item in inventory}
    if not REQUIRED_PACKAGES.issubset(names):
        raise RuntimeError("Windows 运行依赖与轻量清单不一致；缺少："
                           + ", ".join(sorted(REQUIRED_PACKAGES - names)))
    if any(name.startswith(("opencv-", "onnxruntime")) or name in {"shapely", "pyclipper", "indra-ocr", "rapidocr-onnxruntime"}
           for name in names):
        raise RuntimeError("Windows 运行环境包含已移除的 OCR 引擎或图像依赖。")
    if any((site / name).exists() for name in ("cv2", "cv2.py", "shapely", "shapely.py", "onnxruntime", "pyclipper", "rapidocr_onnxruntime", "indra_ocr")):
        raise RuntimeError("Windows 运行环境残留旧 OCR 依赖文件。")
    if (site / "rapidocr" / "models").exists():
        raise RuntimeError("Windows 包残留 RapidOCRLite 自带的非发布模型。")
    return inventory


def prune_runtime(directory: Path):
    site_packages = directory / "Lib/site-packages"
    bundled_models = site_packages / "rapidocr" / "models"
    if bundled_models.is_dir():
        shutil.rmtree(bundled_models)
    for sub in ("f2py", "distutils", "doc", "_core/include", "core/include", "random/lib"):
        target = site_packages / "numpy" / sub
        if target.is_dir():
            shutil.rmtree(target)
    for static_lib in site_packages.rglob("*.lib"):
        static_lib.unlink()
    for avif_ext in (site_packages / "PIL").glob("_avif*"):
        avif_ext.unlink()
    for folder in ("pillow.libs", "Pillow.libs"):
        for avif_lib in (site_packages / folder).glob("*avif*"):
            avif_lib.unlink()
    for path in sorted(directory.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir() and path.name in {"tests", "test", "__pycache__"}:
            shutil.rmtree(path)
        elif path.is_file() and path.suffix == ".pyc":
            path.unlink()


def pe_machine(path: Path) -> int:
    with path.open("rb") as source:
        if source.read(2) != b"MZ":
            raise RuntimeError("Windows 二进制缺少 PE 文件头：" + path.name)
        source.seek(0x3C)
        offset = struct.unpack("<I", source.read(4))[0]
        source.seek(offset)
        if source.read(4) != b"PE\x00\x00":
            raise RuntimeError("Windows 二进制 PE 头无效：" + path.name)
        return struct.unpack("<H", source.read(2))[0]


def verify_payload(payload: Path, inventory: list[dict[str, str]]) -> dict:
    runtime = payload / "runtime"
    rapidocr = runtime / "Lib/site-packages/rapidocr"
    native_libraries = list(rapidocr.rglob("*rapidocrlite*.dll"))
    if not native_libraries:
        raise RuntimeError("Windows 包缺少 RapidOCRLite 原生 CPU 引擎。")
    binaries = [runtime / "python.exe", runtime / "pythonw.exe", *runtime.rglob("*.pyd"), *rapidocr.rglob("*.dll")]
    if any(pe_machine(path) != 0x8664 for path in binaries):
        raise RuntimeError("Windows Python 或扩展不是 AMD64 二进制。")
    if list(payload.rglob("*.so")):
        raise RuntimeError("Windows 包中出现 Linux / Unix .so 文件。")
    models_directory = payload / "receipt_tool" / "models"
    models = list(models_directory.glob("*.onnx"))
    if len(models) != 3 or not list(models_directory.glob("*.txt")):
        raise RuntimeError("OCR 检测、分类、识别模型或字典未完整打包。")
    names = {re.sub(r"[-_.]+", "-", item["name"].lower()) for item in inventory}
    if not REQUIRED_PACKAGES.issubset(names):
        raise RuntimeError("Windows 包缺少核心运行依赖：" + ", ".join(sorted(REQUIRED_PACKAGES - names)))
    return {
        "amd64_executables_and_extensions": len(binaries),
        "ocr_models": len(models),
        "ocr_models_bytes": sum(path.stat().st_size for path in models_directory.iterdir() if path.is_file()),
        "runtime_distributions": len(inventory),
        "required_packages": sorted(REQUIRED_PACKAGES),
        "packages": inventory,
        "ocr_backend": "RapidOCRLite 5.0.0a15 / PP-OCRv6 tiny / native CPU",
        "native_windows_executed": False,
    }


def build(reuse_runtime: bool = False) -> Path:
    if not shutil.which("uv"):
        raise RuntimeError("构建需要 uv。它负责获取适用于 Windows x64 的二进制 wheels。")
    version = (ROOT / "VERSION").read_text("utf-8").strip()
    subprocess.run([sys.executable, str(ROOT / "scripts" / "prepare_ocr_models.py")], check=True)
    stage = ROOT / ".build/windows"
    cache = stage / "downloads"
    payload = stage / "receipt-archive"
    runtime = payload / "runtime"
    cache.mkdir(parents=True, exist_ok=True)
    payload.mkdir(parents=True, exist_ok=True)
    site = runtime / "Lib/site-packages"
    requirements = ROOT / "windows/requirements.lock"
    if not requirements.exists():
        requirements = ROOT / "windows/requirements.txt"
    requirements_digest = digest(requirements)
    requirements_marker = runtime / ".requirements.sha256"
    matching_requirements = (requirements_marker.is_file()
                             and requirements_marker.read_text("ascii").strip() == requirements_digest)
    if not reuse_runtime or not (runtime / "python.exe").exists() or not matching_requirements:
        if runtime.exists():
            shutil.rmtree(runtime)
        runtime.mkdir()
        python_zip = cache / f"python-{PYTHON_VERSION}-embed-amd64.zip"
        download(PYTHON_URL, python_zip, PYTHON_SHA256)
        with ZipFile(python_zip) as source:
            source.extractall(runtime)
        subprocess.run(
            [
                "uv", "pip", "install",
                "--python-platform", "x86_64-pc-windows-msvc",
                "--python-version", "3.13",
                "--target", str(site),
                "--only-binary", ":all:",
                "--no-deps",
                "-r", str(requirements),
            ],
            check=True,
        )
        (runtime / "python313._pth").write_text(
            "python313.zip\n.\n..\nLib/site-packages\nimport site\n", "utf-8"
        )
        requirements_marker.write_text(requirements_digest + "\n", "ascii")
    prune_runtime(runtime)
    inventory = runtime_inventory(site)
    for folder in ("receipt_tool",):
        destination = payload / folder
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(
            ROOT / folder,
            destination,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "requirements.lock"),
        )
    windows = payload / "windows"
    if windows.exists():
        shutil.rmtree(windows)
    windows.mkdir()
    shutil.copy2(ROOT / "windows/launcher.py", windows / "launcher.py")
    for name in ("run.vbs", "run.cmd", "run-browser.cmd"):
        content = (ROOT / "windows" / name).read_text("utf-8").replace("\r\n", "\n")
        (payload / name).write_bytes(content.replace("\n", "\r\n").encode("ascii"))
    shutil.copy2(ROOT / "windows/README.txt", payload / "README-Windows.txt")
    shutil.copy2(ROOT / "README.md", payload / "README.md")
    shutil.copy2(ROOT / "LICENSE", payload / "LICENSE")
    shutil.copy2(ROOT / "VERSION", payload / "VERSION")
    shutil.copy2(requirements, payload / "windows/requirements.lock")
    report = verify_payload(payload, inventory)
    if sys.platform == "win32":
        with tempfile.TemporaryDirectory(prefix="receipt-build-smoke-") as smoke_data:
            environment = {key: value for key, value in os.environ.items()
                           if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}}
            environment.update(RECEIPT_DATA_DIR=smoke_data, PYTHONDONTWRITEBYTECODE="1")
            subprocess.run([str(runtime / "python.exe"), "-c",
                "import numpy; from receipt_tool.recognition import _get_engine; "
                "engine = _get_engine(); import receipt_tool.app, receipt_tool.exporting; "
                "result = engine(numpy.full((96, 192, 3), 255, dtype=numpy.uint8)); "
                "assert result.boxes is None or len(result.boxes) == 0; "
                "print('Lightweight packaged OCR inference ready')"],
                cwd=payload, check=True, env=environment)
        report["native_windows_executed"] = True
        report["ocr_blank_image_inference"] = True
    else:
        report["ocr_blank_image_inference"] = False
    report.update({
        "version": version,
        "platform": "Windows 10/11 x64",
        "python": PYTHON_VERSION,
        "python_archive": PYTHON_URL,
        "python_archive_sha256": PYTHON_SHA256,
        "upstream_sources": [
            "https://docs.python.org/3/using/windows.html#the-embeddable-package",
        ],
        "system_dependencies": ["Microsoft Edge / WebView2", "Microsoft Visual C++ v14 x64 Runtime"],
    })
    (payload / "BUILD-INFO.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    forbidden = {".data", "artifacts", "examples", ".pytest_cache", ".venv"}
    if any(forbidden.intersection(path.relative_to(payload).parts) for path in payload.rglob("*")):
        raise RuntimeError("发现不应发布的数据或开发目录。")
    output = ROOT / "dist/windows" / "凭证归档.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(payload.rglob("*")):
            if path.is_file():
                archive.write(path, Path("凭证归档") / path.relative_to(payload))
    output.with_suffix(".zip.sha256").write_text(f"{digest(output)}  {output.name}\n", "utf-8")
    print(json.dumps({"archive": str(output), "size_mib": round(output.stat().st_size / 2**20, 1), **report}, ensure_ascii=False, indent=2))
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reuse-runtime", action="store_true", help="复用已准备的 Windows 运行依赖，仅刷新项目文件")
    args = parser.parse_args()
    try:
        build(args.reuse_runtime)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print("Windows 打包失败：" + str(error), file=sys.stderr)
        raise SystemExit(1)
