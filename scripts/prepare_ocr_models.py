#!/usr/bin/env python3
"""Fetch the four pinned offline OCR assets; the installed app never downloads models."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
from urllib.request import urlopen


MODELS = Path(__file__).resolve().parents[1] / "receipt_tool" / "models"


def prepare_models(verify_only: bool = False) -> None:
    manifest = json.loads((MODELS / "MODELS.json").read_text("utf-8"))
    for item in manifest["files"]:
        target = MODELS / item["name"]
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == item["sha256"]:
            continue
        if verify_only:
            raise RuntimeError("OCR 模型缺失或校验失败：" + item["name"])
        with tempfile.TemporaryDirectory(prefix=".download-", dir=MODELS) as temporary:
            downloaded = Path(temporary) / item["name"]
            with urlopen(item["url"], timeout=90) as response:
                downloaded.write_bytes(response.read())
            if hashlib.sha256(downloaded.read_bytes()).hexdigest() != item["sha256"]:
                raise RuntimeError("OCR 模型 SHA-256 校验失败：" + item["name"])
            downloaded.replace(target)
    total = sum((MODELS / item["name"]).stat().st_size for item in manifest["files"])
    print(f"离线 OCR 模型校验通过：{len(manifest['files'])} 个文件，{total / 2**20:.2f} MiB")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true")
    prepare_models(parser.parse_args().verify_only)
