#!/usr/bin/env python3
"""Build a lightweight, relocatable offline desktop package for Ubuntu LTS releases."""
from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "receipt-archive"
PYTHON_VERSION = "3.12.13"
PYTHON_KEY = f"cpython-{PYTHON_VERSION}-linux-x86_64-gnu"
WHEEL_PLATFORM = "x86_64-manylinux_2_31"
REQUIRED_PACKAGES = {"rapidocrlite", "numpy", "pillow", "pyyaml", "requests", "tqdm"}


def runtime_inventory(site: Path) -> list[dict[str, str]]:
    inventory = []
    for metadata in sorted(site.glob("*.dist-info/METADATA")):
        parsed = Parser().parsestr(metadata.read_text("utf-8"))
        inventory.append({"name": parsed["Name"], "version": parsed["Version"]})
    names = {re.sub(r"[-_.]+", "-", item["name"].lower()) for item in inventory}
    if not REQUIRED_PACKAGES.issubset(names):
        raise RuntimeError("Linux 运行依赖与轻量清单不一致；缺少："
                           + ", ".join(sorted(REQUIRED_PACKAGES - names)))
    if any(name.startswith(("opencv-", "onnxruntime")) or name in {"shapely", "pyclipper", "indra-ocr", "rapidocr-onnxruntime"}
           for name in names):
        raise RuntimeError("Linux 运行环境包含已移除的 OCR 引擎或图像依赖。")
    if any((site / name).exists() for name in ("cv2", "cv2.py", "shapely", "shapely.py", "onnxruntime", "pyclipper", "rapidocr_onnxruntime", "indra_ocr")):
        raise RuntimeError("Linux 运行环境残留旧 OCR 依赖文件。")
    if (site / "rapidocr" / "models").exists():
        raise RuntimeError("Linux 包残留 RapidOCRLite 自带的非发布模型。")
    return inventory


def read_version() -> str:
    version_file = ROOT / "VERSION"
    if not version_file.is_file():
        raise RuntimeError("缺少项目 VERSION 文件。")
    version = version_file.read_text("utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[+~-][A-Za-z0-9.+~]+)?", version):
        raise RuntimeError("VERSION 应为有效版本号，例如 1.3.0。")
    return version


def copy_sources(payload: Path, version: str):
    source = ROOT / "receipt_tool"
    if not source.is_dir():
        raise RuntimeError(f"缺少源代码目录：{source}")
    target = payload / "receipt_tool"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    desktop = payload / "desktop"
    if desktop.exists():
        shutil.rmtree(desktop)
    desktop.mkdir()
    for filename in ("launcher.py", "receipt_desktop.py", "drop_import.py", "icon.svg"):
        source_file = ROOT / "desktop" / filename
        if not source_file.is_file():
            raise RuntimeError(f"缺少桌面运行文件：{source_file}")
        shutil.copy2(source_file, desktop / filename)
    shutil.copy2(ROOT / "README.md", payload / "README.md")
    (payload / "VERSION").write_text(version + "\n", "utf-8")


def prune_runtime(runtime: Path):
    """Strip unused stdlib GUI/pip modules, static C libraries, AV1 codecs, and test suites."""
    stdlib = runtime / "lib" / "python3.12"
    site_packages = stdlib / "site-packages"

    # 1. Remove unused CPython stdlib modules (~12 MiB: tkinter, ensurepip, idlelib, etc.)
    for std_dir in ("ensurepip", "tkinter", "turtledemo", "idlelib", "pydoc_data", "lib2to3", "venv"):
        target = stdlib / std_dir
        if target.is_dir():
            shutil.rmtree(target)
    for config_dir in stdlib.glob("config-3.12*"):
        if config_dir.is_dir():
            shutil.rmtree(config_dir)
    if (stdlib / "turtle.py").is_file():
        (stdlib / "turtle.py").unlink()

    # Remove bundled Tcl/Tk shared libraries (Linux GUI uses GTK3/WebKitGTK)
    lib_dir = runtime / "lib"
    for pattern in ("libtk*", "libtcl*", "tk[89]*", "tcl[89]*", "itcl*", "thread*"):
        for item in lib_dir.glob(pattern):
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink(missing_ok=True)

    # This standalone interpreter embeds CPython. Its unused shared copy is
    # needed only by external embedding applications, not this bundled runtime.
    readelf = shutil.which("readelf")
    if readelf:
        dynamic = subprocess.run([readelf, "-d", str(runtime / "bin" / "python3.12")],
                                 capture_output=True, text=True, check=True).stdout
        if "libpython" not in dynamic:
            for library in lib_dir.glob("libpython*.so*"):
                library.unlink()

    # 2. Remove pip/wheel/setuptools if bundled in site-packages (~10 MiB)
    for pkg_pattern in ("pip", "pip-*", "setuptools", "setuptools-*", "wheel", "wheel-*"):
        for item in site_packages.glob(pkg_pattern):
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink(missing_ok=True)

    # Models are pinned separately under receipt_tool/models; discard bundled alternatives.
    bundled_models = site_packages / "rapidocr" / "models"
    if bundled_models.is_dir():
        shutil.rmtree(bundled_models)

    # 4. Prune NumPy Fortran compiler, C headers, and static .a libraries (~3.5 MiB)
    for sub in ("f2py", "distutils", "doc", "_core/include", "core/include", "random/lib"):
        target = site_packages / "numpy" / sub
        if target.is_dir():
            shutil.rmtree(target)
    for static_lib in site_packages.rglob("*.a"):
        static_lib.unlink()

    # 5. Prune unused AV1 codec library from Pillow.libs (~4.5 MiB; app supports PNG/JPG/WEBP/TIFF/BMP)
    for folder in ("pillow.libs", "Pillow.libs"):
        for avif_lib in (site_packages / folder).glob("libavif*"):
            avif_lib.unlink()
    for avif_ext in (site_packages / "PIL").glob("_avif*"):
        avif_ext.unlink()

    # 6. Remove test directories while preserving .dist-info and licenses
    for directory, children, _files in os.walk(runtime):
        for name in list(children):
            if name.endswith(".dist-info") or name.lower() in {"license", "licenses"}:
                children.remove(name)
            elif name in {"test", "tests", "__pycache__"}:
                shutil.rmtree(Path(directory) / name)
                children.remove(name)
    for pyc in runtime.rglob("*.pyc"):
        pyc.unlink()

    # 7. Strip unneeded debug symbols from shared objects if `strip` is available
    strip_bin = shutil.which("strip")
    if strip_bin:
        for so_file in runtime.rglob("*.so*"):
            if so_file.is_file() and not so_file.is_symlink():
                subprocess.run([strip_bin, "--strip-unneeded", str(so_file)], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def prepare_runtime(payload: Path, skip_install: bool):
    runtime = payload / "runtime"
    python = runtime / "bin" / "python"
    if runtime.exists() and ((runtime / "pyvenv.cfg").exists()
                             or not (runtime / "lib" / "python3.12" / "os.py").is_file()):
        shutil.rmtree(runtime)
    if not python.exists():
        managed = ROOT / ".build" / "python"
        subprocess.run(["uv", "python", "install", "--install-dir", str(managed),
                        "--no-bin", PYTHON_VERSION], check=True)
        shutil.copytree(managed / PYTHON_KEY, runtime, symlinks=True)
    notices = runtime / "licenses" / "python-build-standalone"
    if not notices.is_dir():
        build_tag = (runtime / "BUILD").read_text("utf-8").strip()
        source_archive = ROOT / ".build" / f"python-build-standalone-{build_tag}.tar.gz"
        if not source_archive.is_file():
            source_url = f"https://codeload.github.com/astral-sh/python-build-standalone/tar.gz/refs/tags/{build_tag}"
            with urlopen(source_url, timeout=45) as source, source_archive.open("wb") as target:
                shutil.copyfileobj(source, target)
        with tarfile.open(source_archive) as archive:
            notices.mkdir(parents=True)
            for member in archive:
                parts = Path(member.name).parts
                if member.isfile() and len(parts) == 2 and (parts[1].startswith("LICENSE")
                                                           or parts[1] == "python-licenses.rst"):
                    (notices / parts[1]).write_bytes(archive.extractfile(member).read())
    requirements = ROOT / "requirements-linux.lock"
    if not requirements.exists():
        requirements = ROOT / "requirements.txt"
    site_packages = runtime / "lib/python3.12/site-packages"
    if not skip_install:
        # uv --target installs requested packages but does not remove old dependencies.
        if site_packages.exists():
            shutil.rmtree(site_packages)
        site_packages.mkdir(parents=True)
        subprocess.run(["uv", "pip", "install", "--python", str(python),
            "--python-platform", WHEEL_PLATFORM, "--python-version", "3.12",
            "--target", str(site_packages), "--only-binary", ":all:", "--no-deps",
            "-r", str(requirements)], check=True)
    prune_runtime(runtime)
    for path in (runtime / "bin").iterdir():
        if path.name not in {"python", "python3", "python3.12"}:
            path.unlink()
    for directory in (runtime / "include", runtime / "share" / "man"):
        if directory.is_dir():
            shutil.rmtree(directory)
    for archive in (runtime / "lib").glob("libpython*.a"):
        archive.unlink()
    shutil.copy2(ROOT / "requirements-linux.lock", payload / "requirements-linux.lock")
    return runtime


def build(skip_install: bool = False) -> Path:
    if platform.machine() not in ("x86_64", "amd64"):
        raise RuntimeError("此安装包面向 Ubuntu x86_64，需要在相同架构上构建。")
    version = read_version()
    subprocess.run([sys.executable, str(ROOT / "scripts" / "prepare_ocr_models.py")], check=True)
    staging = ROOT / ".build" / "package-root"
    payload = staging / "opt" / PACKAGE
    payload.mkdir(parents=True, exist_ok=True)
    runtime = prepare_runtime(payload, skip_install)
    site_packages = runtime / "lib/python3.12/site-packages"
    inventory = runtime_inventory(site_packages)
    copy_sources(payload, version)
    models_directory = payload / "receipt_tool" / "models"
    models = list(models_directory.glob("*.onnx"))
    if len(models) != 3 or not list(models_directory.glob("*.txt")):
        raise RuntimeError("OCR 检测、分类、识别模型或字典未完整打包。")
    native_libraries = list((site_packages / "rapidocr").rglob("*rapidocrlite*.so*"))
    if not native_libraries:
        raise RuntimeError("Linux 包缺少 RapidOCRLite 原生 CPU 引擎。")
    command = staging / "usr" / "bin" / PACKAGE
    command.parent.mkdir(parents=True, exist_ok=True)
    command.write_text('#!/bin/sh\nexec /usr/bin/python3 /opt/receipt-archive/desktop/launcher.py "$@"\n', "utf-8")
    command.chmod(0o755)
    applications = staging / "usr" / "share" / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    entry = applications / f"{PACKAGE}.desktop"
    entry.write_text("[Desktop Entry]\nType=Application\nName=凭证归档\n"
        "GenericName=订单与支付凭证整理\nComment=识别截图、自动配对，导出 Word 和 PDF\n"
        "Exec=/usr/bin/receipt-archive\nTryExec=/usr/bin/receipt-archive\n"
        "Icon=receipt-archive\nTerminal=false\nCategories=Office;Finance;\n"
        "StartupNotify=true\nStartupWMClass=ReceiptArchive\n", "utf-8")
    icon = staging / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps" / f"{PACKAGE}.svg"
    icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "desktop" / "icon.svg", icon)
    documentation = staging / "usr" / "share" / "doc" / PACKAGE
    documentation.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "README.md", documentation / "README.md")
    shutil.copy2(ROOT / "LICENSE", documentation / "LICENSE")
    size_kb = sum(path.lstat().st_size for path in staging.rglob("*")
                  if path.is_file() or path.is_symlink()) // 1024
    control = staging / "DEBIAN" / "control"
    control.parent.mkdir(parents=True, exist_ok=True)
    control.write_text(f"Package: {PACKAGE}\nVersion: {version}\nSection: office\nPriority: optional\n"
        "Architecture: amd64\nMaintainer: ZzxArthur <ZzxArthur@users.noreply.github.com>\n"
        f"Installed-Size: {size_kb}\n"
        "Depends: python3 (>= 3.8), python3-gi, gir1.2-gtk-3.0, "
        "gir1.2-webkit2-4.1 (>= 2.36) | gir1.2-webkit2-4.0 (>= 2.36), "
        "libglib2.0-0 (>= 2.64) | libglib2.0-0t64, librsvg2-common, libgomp1, libstdc++6, libc6 (>= 2.31), zlib1g\n"
        "Description: 凭证归档 - lightweight local screenshot receipt organizer\n"
        " Native Linux desktop application with offline Chinese OCR, screenshot\n"
        " matching, duplicate detection, and zero-dependency Word/PDF export.\n", "utf-8")
    for path in staging.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        elif not path.is_symlink():
            executable = path == command or (path.parent == runtime / "bin"
                                             and path.name in {"python", "python3", "python3.12"})
            path.chmod(0o755 if executable else 0o644)
    # Relocation and offline model checks happen against the exact payload.
    with tempfile.TemporaryDirectory(prefix="receipt-build-smoke-") as smoke_data:
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH"}}
        environment.update(RECEIPT_DATA_DIR=smoke_data, PYTHONDONTWRITEBYTECODE="1")
        subprocess.run([str(runtime / "bin" / "python"), "-c",
            "import numpy; from receipt_tool.recognition import _get_engine; "
            "engine = _get_engine(); import receipt_tool.app, receipt_tool.exporting; "
            "result = engine(numpy.full((96, 192, 3), 255, dtype=numpy.uint8)); "
            "assert result.boxes is None or len(result.boxes) == 0; "
            "print('Lightweight packaged OCR inference ready')"],
            cwd=payload, check=True, env=environment)
    (payload / "BUILD-INFO.json").write_text(json.dumps({
        "version": version, "python": PYTHON_VERSION, "architecture": "amd64",
        "python_build": (runtime / "BUILD").read_text("utf-8").strip(),
        "runtime": "python-build-standalone-slim", "wheel_platform": WHEEL_PLATFORM,
        "ocr_backend": "RapidOCRLite 5.0.0a15 / PP-OCRv6 tiny / native CPU",
        "ocr_models": len(models), "runtime_distributions": len(inventory),
        "ocr_models_bytes": sum(path.stat().st_size for path in models_directory.iterdir() if path.is_file()),
        "required_packages": sorted(REQUIRED_PACKAGES), "packages": inventory,
        "ocr_blank_image_inference": True,
        "system_python_minimum": "3.8", "webkit_minimum": "2.36", "webkit_abis": ["4.1", "4.0"],
        "upstream_sources": ["https://github.com/astral-sh/python-build-standalone",
            "https://docs.astral.sh/uv/concepts/python-versions/"],
    }, ensure_ascii=False, indent=2) + "\n", "utf-8")
    subprocess.run(["desktop-file-validate", str(entry)], check=True)
    for path in payload.rglob("__pycache__"):
        shutil.rmtree(path)
    output = ROOT / "dist" / "ubuntu" / "凭证归档.deb"
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "-z6", "--build", str(staging), str(output)], check=True)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(f"{digest}  {output.name}\n", "utf-8")
    print(f"安装包：{output}\n大小：{output.stat().st_size / 1024 / 1024:.1f} MiB")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-install", action="store_true", help="复用已经准备好的纯运行环境")
    args = parser.parse_args()
    try:
        build(args.skip_install)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"构建失败：{error}", file=sys.stderr)
        raise SystemExit(1)
