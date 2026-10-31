# Receipt Archive

Turn scattered **order screenshots** and **payment screenshots** into a clean, printable reimbursement packet.

Offline OCR · Local-only · Automatic matching · Word / PDF export

[中文说明](README.md) · [MIT License](LICENSE)

## What it does

Reimbursement paperwork usually fails at the same step: pairing each order with the payment that paid for it.

- The order lives in one screenshot and the payment receipt in another.
- Dozens of screenshots arrive out of order.
- The same image is saved and sent more than once.
- One order may be paid in two or three installments.
- The final packet must place the order above its payment receipts, one order per page.

Receipt Archive reduces this to **import → review → export**, entirely on your machine.

It is built for Chinese order/payment pages (Didi, Amap, Meituan, 12306, WeChat Pay, Alipay, and similar), and it handles **reimbursement evidence**, not VAT invoices. Digital invoices (PDF / OFD / XML), tax verification, and expense forms are not in scope yet — see the [roadmap](README.md#路线图).

## Features

- Drag and drop, file picker, or paste with `Ctrl / ⌘ + V`; add more screenshots to the same batch at any time.
- Pixel-level duplicate detection: identical images are skipped even under a different filename.
- Offline OCR with RapidOCRLite (native CPU) and PP-OCRv6 tiny models. No cloud, no API key.
- Classifies each image as order, payment, or unknown; extracts the actual paid amount, date, time, merchant, and order number.
- Conservative matching by amount, order number, date, merchant, and time. Supports up to four split payments per order and only auto-confirms a **unique** combination.
- Manual correction for every field and every pairing. Manual edits survive re-recognition.
- A4 DOCX / PDF: one order per page, order on top, payment receipts below, optional cropping of promotional footers.
- Batches survive window closes and upgrades.

## Install

Download the package for your platform from [Releases](https://github.com/ZzxArthur/receipt-archive/releases):

| Platform | File | Notes |
| --- | --- | --- |
| Ubuntu 20.04 / 22.04 / 24.04 LTS (x86_64) | `凭证归档.deb` | `sudo apt install ./凭证归档.deb` |
| Windows 10 / 11 (x64) | `凭证归档.zip` | Extract fully, then run `run.vbs` |

Binary packages are built from this repository and are not committed to Git. To run from source you need Python 3.12 and [uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --no-deps -r requirements-linux.lock
.venv/bin/python scripts/prepare_ocr_models.py
.venv/bin/python -m receipt_tool.app --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765>. On Ubuntu, `./desktop.sh` opens the native GTK/WebKit window instead.

## Design constraints

These are deliberate, not stylistic preferences:

- **Offline by default.** No cloud OCR, telemetry, accounts, or API keys.
- **Integer cents.** Amounts are never parsed or summed as floats.
- **Uncertainty is allowed.** Low-confidence fields and ambiguous matches go to manual review instead of being guessed.
- **Never read the phone status-bar clock.** Only times attached to transaction or trip labels are used.
- **Local-only HTTP.** The service binds to loopback and validates the `Host` header.

## Development

```bash
uv pip install --python .venv/bin/python --no-deps -r requirements-linux.lock
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q receipt_tool desktop windows scripts
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow and [SECURITY.md](SECURITY.md) for the threat model.

## License

Code is licensed under the [MIT License](LICENSE). OCR uses [RapidOCRLite](https://github.com/RapidAI/RapidOCRLite); the PP-OCR weights originate from PaddleOCR and are redistributed under Apache-2.0 with attribution in `receipt_tool/models/`.
