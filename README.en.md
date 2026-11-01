# Receipt Archive

Turn scattered **order screenshots** and **payment screenshots** into a clean, printable reimbursement packet.

Offline OCR · Local-only · Automatic matching · Word / PDF export

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB.svg)
![Platform](https://img.shields.io/badge/Platform-Ubuntu%20%7C%20Windows-4B5563.svg)

[中文说明](README.md) · [Quick start](#quick-start) · [Usage](#usage) · [Limitations](#limitations)

> This tool prepares **reimbursement evidence**, not VAT invoices. Digital invoices (PDF / OFD / XML), tax verification, bookkeeping, and tax filing are out of scope.

---

## What it does

Reimbursement paperwork usually fails at the same step: pairing each order with the payment that paid for it.

- The order lives in one screenshot and the payment receipt in another.
- Dozens of screenshots arrive out of order.
- The same image is saved and sent more than once.
- One order may be paid in two or three installments.
- The final packet must place the order above its payment receipts, one order per page.

Receipt Archive reduces this to **import → review → export**, entirely on your machine. It is built for Chinese order/payment pages (Didi, Amap, Meituan, 12306, WeChat Pay, Alipay, and similar).

## Features

- Drag and drop, file picker, or paste with `Ctrl / ⌘ + V`; add more screenshots to the same batch at any time.
- Pixel-level duplicate detection: identical images are skipped even under a different filename.
- Offline OCR with RapidOCRLite (native CPU) and PP-OCRv6 tiny models. No cloud, no API key.
- Classifies each image as order, payment, or unknown; extracts the actual paid amount, date, time, merchant, and order number.
- Conservative matching by amount, order number, date, merchant, and time. Supports up to four split payments per order and only auto-confirms a **unique** combination.
- Manual correction for every field and every pairing. Manual edits survive re-recognition.
- A4 DOCX / PDF: one order per page, order on top, payment receipts below, optional cropping of promotional footers.
- Batches survive window closes and upgrades.

Limits per batch: **500 images, 20 MB each, 512 MB total**.

## Quick start

### Option 1: Download a package

Download the package for your platform from [Releases](https://github.com/ZzxArthur/receipt-archive/releases):

| Platform | File | Notes |
| --- | --- | --- |
| Ubuntu 20.04 / 22.04 / 24.04 LTS (x86_64) | `凭证归档.deb` | `sudo apt install ./凭证归档.deb` |
| Windows 10 / 11 (x64) | `凭证归档.zip` | Extract fully, then run `run.vbs` |

Ubuntu needs GTK 3 / WebKitGTK 2.36+ (apt handles the dependency). Windows needs the Microsoft Visual C++ v14 x64 Runtime.

> Binary packages are not committed to Git. If Releases has no package yet, run from source.

### Option 2: Run from source

You need Python 3.12, [uv](https://docs.astral.sh/uv/), and network access for the first install:

```bash
git clone https://github.com/ZzxArthur/receipt-archive.git
cd receipt-archive

uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --no-deps -r requirements-linux.lock
.venv/bin/python scripts/prepare_ocr_models.py

# Browser mode
.venv/bin/python -m receipt_tool.app --host 127.0.0.1 --port 8765
# Open http://127.0.0.1:8765

# Or the Ubuntu native window (GTK 3 / WebKitGTK)
./desktop.sh
```

`./start.sh` creates the environment, prepares the models, and starts in the foreground. `./open.sh` starts in the background and opens the browser.

### Option 3: Build a package

```bash
python3 scripts/build_linux_package.py    # Ubuntu .deb
python3 scripts/build_windows_package.py  # Windows portable ZIP
```

Outputs go to `dist/ubuntu/` and `dist/windows/` with SHA-256 checksums.

## Usage

1. **Import.** Drag order and payment screenshots into the window, pick files, or paste. Order does not matter; you can add screenshots in several rounds.
2. **Recognize.** Click “开始识别” (Start recognition). Each image is OCR'd locally. Type, amount, date, merchant, and order number are extracted.
3. **Review.** Open the “识别结果” (Results) tab and check the type, the actual paid amount, and the date/merchant. Every field is editable.
4. **Pair.** Open “配对预览” (Pairs). A green “已配对” group is confirmed; a yellow “需要核对” group needs attention. Use “核对 / 调整配对” to assign an order and one or more payments manually.
5. **Export.** Choose Word + PDF (ZIP), DOCX, or PDF, optionally add a title and crop promotional footers. Export is blocked while items are unreviewed unless you tick “同时导出待确认与未配对截图”.

One order supports up to four split payments. Matching is confirmed automatically only when the sum is exact and the combination is unique.

The app keeps **one active batch** at a time. Export first, then click “清空本批次” (Clear batch) before starting the next one. Clearing deletes that batch's screenshots and OCR results from the working directory.

## Limitations

| Limitation | What it means | Workaround |
| --- | --- | --- |
| **Chinese order/payment pages only** | Built-in merchant aliases cover Didi, Amap, Meituan, 12306, and a few others. Other languages/layouts may land in “unknown”. | Correct the type/amount manually, or add rules in `recognition.py`. |
| **Screenshots only** | PNG / JPEG / WEBP / TIFF / BMP. No PDF, OFD, or XML e-invoices. | Convert e-invoices to images, or use a dedicated invoice tool. |
| **Limited OCR accuracy** | The tiny model can fail on low-resolution, heavily compressed, busy, or stylized screenshots. | Use the original image; fill in fields manually when needed. |
| **Amounts must match exactly** | A one-cent difference, partial refund, discount mismatch, or foreign currency will not auto-match. | Pair manually; do not edit amounts just to force a match. |
| **One payment belongs to one order** | A single payment covering several orders (shopping-cart checkout) is not supported. | Split the payment screenshot per order, or organize them separately. |
| **No status-bar clock** | If the screenshot has no “payment time / order time / trip end” label, the date may be empty. | Enter the transaction date manually. |
| **One batch at a time** | Clearing the batch is required before starting a new one, and it deletes that batch's data. | Export first; keep a backup of important screenshots. |
| **Batch size limits** | 500 images, 20 MB per image, 512 MB total. | Use one batch per month or per project. |
| **Windows package is cross-built** | It has not been fully regression-tested on Windows 10/11. | Prefer running from source, or test on a spare machine. |
| **Pre-release OCR engine** | Pinned to `RapidOCRLite==5.0.0a15`; behavior may change on upgrade. | Re-run your own screenshot samples before upgrading. |
| **No mobile or remote access** | HTTP binds to `127.0.0.1`; a phone cannot connect directly. | Use the desktop app; transfer screenshots to the computer first. |

### Explicitly out of scope

- **Tax verification.** The national VAT invoice verification portal requires a CAPTCHA and has no public personal API. Bypassing it is neither stable nor compliant.
- **Digital invoice parsing.** PDF / OFD / XML e-invoices are not read.
- **Bookkeeping, tax filing, invoicing.** This is a reimbursement packet builder, not an accounting system.
- **Uploading images.** No cloud OCR, telemetry, accounts, or API keys.

## Data and privacy

| Platform | Screenshots and OCR data | Logs |
| --- | --- | --- |
| Ubuntu | `~/.local/share/receipt-archive/data/` | `~/.local/state/receipt-archive/` |
| Windows | `%LOCALAPPDATA%\ReceiptArchive\data\` | Same directory: `backend.log` / `desktop.log` |

Set `RECEIPT_DATA_DIR` to override the data location. To back up, copy the whole `data/` directory. To move machines, put it back at the same path.

OCR, matching, DOCX, and PDF generation all run locally. The only network access is the first source install (dependencies and pinned OCR models); release packages bundle the models and run offline.

## Design constraints

These are deliberate, not stylistic preferences:

- **Offline by default.** No cloud OCR, telemetry, accounts, or API keys.
- **Integer cents.** Amounts are never parsed or summed as floats.
- **Uncertainty is allowed.** Low-confidence fields and ambiguous matches go to manual review instead of being guessed.
- **Never read the phone status-bar clock.** Only times attached to transaction or trip labels are used.
- **Local-only HTTP.** The service binds to loopback and validates the `Host` header.

## Development

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --no-deps -r requirements-linux.lock

.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q receipt_tool desktop windows scripts
```

Tests need no network and no real screenshots: OCR tests use constructed text boxes, and export tests use generated solid-color images.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow and [SECURITY.md](SECURITY.md) for the threat model.

## License

Code is licensed under the [MIT License](LICENSE). OCR uses [RapidOCRLite](https://github.com/RapidAI/RapidOCRLite); the PP-OCR weights originate from PaddleOCR and are redistributed under Apache-2.0 with attribution in `receipt_tool/models/`.
