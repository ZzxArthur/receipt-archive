#!/usr/bin/env python3
"""Install the prepared native app for the current user without root access."""
from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, default=ROOT / ".build/package-root/opt/receipt-archive")
    parser.add_argument("--desktop", action="store_true", help="同时添加桌面图标")
    args = parser.parse_args()
    source = args.payload.resolve()
    if not (source / "runtime/bin/python").exists() or not (source / "desktop/launcher.py").is_file():
        raise SystemExit("请先运行 scripts/build_linux_package.py 准备安装内容。")
    home = Path.home()
    configured_data = os.environ.get("XDG_DATA_HOME", "")
    data_home = Path(configured_data) if configured_data and Path(configured_data).is_absolute() else home / ".local/share"
    base = data_home / "receipt-archive"
    base.mkdir(parents=True, exist_ok=True)
    destination = base / "app"
    stage = base / "app.installing"
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(source, stage, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    data = base / "data"
    previous = None
    if destination.exists():
        previous = base / ("app.previous-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        destination.replace(previous)
    stage.replace(destination)
    command_dir = home / ".local/bin"
    command_dir.mkdir(parents=True, exist_ok=True)
    command = command_dir / "receipt-archive"
    # The executable path is supplied as a literal shell argument.
    import shlex
    command.write_text("#!/bin/sh\nexec /usr/bin/python3 " + shlex.quote(str(destination / "desktop/launcher.py")) + ' "$@"\n', "utf-8")
    command.chmod(0o755)
    applications = data_home / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    icons = data_home / "icons/hicolor/scalable/apps"
    icons.mkdir(parents=True, exist_ok=True)
    shutil.copy2(destination / "desktop/icon.svg", icons / "receipt-archive.svg")
    entry = applications / "receipt-archive.desktop"
    # Desktop Exec has its own double-quote escaping rules.
    escaped_command = str(command).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    entry.write_text("[Desktop Entry]\nType=Application\nName=凭证归档\n"
        "Comment=识别截图、自动配对，导出 Word 和 PDF\n"
        f'Exec="{escaped_command}"\nIcon=receipt-archive\nTerminal=false\n'
        "Categories=Office;Finance;\nStartupNotify=true\nStartupWMClass=ReceiptArchive\n", "utf-8")
    subprocess.run(["desktop-file-validate", str(entry)], check=True)
    if args.desktop:
        try:
            desktop = Path(subprocess.check_output(["xdg-user-dir", "DESKTOP"], text=True).strip())
            if desktop.is_dir() and desktop != home:
                shortcut = desktop / "凭证归档.desktop"
                shutil.copy2(entry, shortcut)
                shortcut.chmod(0o755)
                subprocess.run(["gio", "set", str(shortcut), "metadata::trusted", "true"], capture_output=True)
        except (OSError, subprocess.SubprocessError):
            pass
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(applications)], check=False)
    print(f"已安装：{destination}\n启动命令：{command}\n截图数据：{data}")
    if previous:
        print(f"旧版代码备份：{previous}")


if __name__ == "__main__":
    main()
