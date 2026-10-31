"""Offline screenshot OCR and deliberately conservative receipt matching.

The OCR engine is imported and initialised only on the first recognition request.
All amounts are integer fen: neither matching nor addition uses floating point.
"""

from __future__ import annotations

from dataclasses import dataclass
from bisect import bisect_left, bisect_right
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import reduce
from math import gcd
import os
from pathlib import Path
import re
import threading
import unicodedata
from typing import Any


_engine: Any = None
_engine_lock = threading.Lock()
_inference_lock = threading.Lock()


def _get_engine() -> Any:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                try:
                    requested = int(os.environ.get("RECEIPT_OCR_THREADS", "2"))
                except ValueError:
                    requested = 2
                threads = min(4, os.cpu_count() or 1, max(1, requested))
                os.environ.setdefault("PPOCR_BENCH_THREADS", str(threads))
                from rapidocr import RapidOCR

                models = Path(__file__).resolve().parent / "models"
                filenames = ("PP-OCRv6_det_tiny.onnx", "PP-OCRv6_rec_tiny.onnx",
                             "ppocrv6_tiny_dict.txt", "ch_ppocr_mobile_v2.0_cls_infer.onnx")
                if not all((models / name).is_file() for name in filenames):
                    raise RuntimeError("离线 OCR 模型不完整，请重新解压或安装应用。")
                _engine = RapidOCR(params={
                    "Global.model_root_dir": str(models),
                    "Global.use_cls": True,
                    "Det.model_type": "tiny",
                    "Rec.model_type": "tiny",
                    "Det.model_path": str(models / filenames[0]),
                    "Rec.model_path": str(models / filenames[1]),
                    "Rec.rec_keys_path": str(models / filenames[2]),
                    "Cls.model_path": str(models / filenames[3]),
                    "EngineConfig.ppocr_cpp.backend": "cpu",
                })
    return _engine


@dataclass(frozen=True)
class _Line:
    text: str
    score: float
    left: float
    top: float
    right: float
    bottom: float

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def height(self) -> float:
        return max(0.001, self.bottom - self.top)


_MONEY = re.compile(
    r"(?<![\d.])(?P<sign>[-−﹣]?)[ \t]*(?P<currency>[¥￥]?)"
    r"[ \t]*(?P<number>\d{1,7}(?:[,，]\d{3})*(?:[.．]\d{1,2})?)"
    r"(?P<unit>[ \t]*元)?(?![\d.])"
)
_DISCOUNT = re.compile(r"支付立减|共减|立减|满减|直减|优惠|抵扣|减免|补贴|已省|立省|节省|红包|折扣|特惠|券|奖励|积分|返现|减")
_BREAKDOWN = re.compile(r"起步价|里程费|时长费|远途费|路桥费|高速费|服务费|调度费|等待费|原价|单价|商品金额|商品总额|市场价|划线价|折前价|到手价|到手")
_ACTUAL = re.compile(r"实付款(?:金额)?|实付(?:金额|款)?|实际(?:支付|付款|扣款)|实收(?:金额)?|支付金额|付款金额|扣款金额|合计|总计|订单金额|应付(?:金额)?")
_BALANCE = re.compile(r"余额|可用额度|累计|贷款额度|授信额度")
_MARKETING = re.compile(r"至高|最高可|获取|领取|限时抢|新客专享|满$")
_PROMO = re.compile(r"活动专区|福利中心|更多优惠|猜你喜欢|为你推荐|为您推荐")
_DATE = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?")
_TIME = re.compile(r"(?<!\d)([0-2]?\d)[:：]([0-5]\d)(?:[:：]([0-5]\d))?(?!\d)")


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def _normalise_lines(raw: list, width: int, height: int) -> list[_Line]:
    lines: list[_Line] = []
    for item in raw:
        if not item or len(item) < 3:
            continue
        box, content, confidence = item[:3]
        if not content:
            continue
        xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
        lines.append(_Line(
            unicodedata.normalize("NFKC", str(content)).strip(),
            max(0.0, min(1.0, float(confidence))),
            max(0.0, min(xs) / width), max(0.0, min(ys) / height),
            min(1.0, max(xs) / width), min(1.0, max(ys) / height),
        ))
    return sorted(lines, key=lambda line: (round(line.cy / 0.008), line.left))


def _classify(lines: list[_Line]) -> str:
    body = "\n".join(_compact(line.text) for line in lines if line.top > 0.055)
    if re.search(r"退款成功|退款处理中|支付失败|付款失败|交易关闭|订单取消|待支付|待付款", body):
        # A failed/refunded receipt must never silently become a paid expense.
        return "unknown"
    bill = bool(re.search(r"自动扣款成功|扣款成功|账单详情|交易详情|付款详情|支付凭证|付款凭证", body))
    order = bool(re.search(r"联系司机|费用疑问|呼叫返程|上车|下车|订单详情|订单信息|订单成功|订单完成|行程结束|订单已完成|已签收|已送达|配送完成|确认收货", body))
    if not order and re.search(r"订单(?:编号|号)", body) and _ACTUAL.search(body):
        order = True
    if bill:
        return "payment"
    if order:
        return "order"
    if re.search(r"支付成功|付款成功|交易成功|扣款成功", body):
        return "payment"
    return "unknown"


def _nearest_amount_label(text: str) -> str | None:
    """Return the last semantic field before one number, not every field in a row.

    For example, 优惠后实付 binds to 实付, while 共减¥6合计¥19.9
    assigns 共减 to the first value and 合计 to the second value.
    """
    labels = [(match.start(), match.end(), kind)
              for kind, pattern in (("actual", _ACTUAL), ("discount", _DISCOUNT),
                                    ("breakdown", _BREAKDOWN), ("balance", _BALANCE))
              for match in pattern.finditer(text)]
    return max(labels, default=(0, 0, None), key=lambda label: (label[0], label[1]))[2]


def _spatial_amount_label(line: _Line, token: re.Match, lines: list[_Line]) -> str | None:
    # Estimate the token's part of a merged OCR box. Chinese characters occupy
    # about twice the width of Latin letters/digits, sufficient for row binding.
    def text_width(text: str) -> int:
        return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)

    scale = (line.right - line.left) / max(1, text_width(line.text))
    left = line.left + text_width(line.text[:token.start()]) * scale
    right = line.left + text_width(line.text[:token.end()]) * scale
    same_row = []
    above = []
    for other in lines:
        if other is line or re.search(r"\d", other.text):
            continue
        semantic = _nearest_amount_label(other.text)
        if semantic is None:
            continue
        distance_y = abs(other.cy - line.cy)
        if (other.right <= left + 0.045
                and distance_y <= max(other.height, line.height) * 0.8):
            same_row.append((distance_y, max(0, left - other.right), semantic))
            continue
        gap = line.top - other.bottom
        overlap = min(right, other.right) - max(left, other.left)
        if (0 <= gap <= min(0.045, max(0.018, 1.5 * other.height))
                and overlap >= min(right - left, other.right - other.left) * 0.35):
            above.append((gap, abs((left + right) - (other.left + other.right)), semantic))
    if same_row:
        return min(same_row, key=lambda label: (label[0], label[1]))[2]
    if above:
        return min(above, key=lambda label: (label[0], label[1]))[2]
    return None


def _amounts(lines: list[_Line], kind: str) -> tuple[int | None, list[dict], list[str], _Line | None]:
    candidates: list[dict] = []
    warnings: list[str] = []
    body_heights = sorted(line.height for line in lines if 0.07 < line.cy < 0.94)
    median_height = body_heights[len(body_heights) // 2] if body_heights else 0.02
    promos = [line.top for line in lines if _PROMO.search(line.text) and line.top > 0.45]
    promo_top = min(promos, default=1.0)
    successes = [line for line in lines if re.search(r"支付成功|付款成功|自动扣款成功|交易成功", _compact(line.text))]
    for line in lines:
        if line.top < 0.055 or line.cy >= promo_top:
            continue
        non_money_spans = [match.span() for pattern in (_DATE, _TIME) for match in pattern.finditer(line.text)]
        previous_number_end = 0
        for token in _MONEY.finditer(line.text):
            local_prefix = line.text[previous_number_end:token.start()]
            previous_number_end = token.end()
            if any(token.start() < end and token.end() > start for start, end in non_money_spans):
                continue
            raw_number = token.group("number").replace(",", "").replace("，", "").replace("．", ".")
            before, after = line.text[:token.start()], line.text[token.end():]
            semantic = _nearest_amount_label(local_prefix)
            if semantic is None:
                semantic = _spatial_amount_label(line, token, lines)
            if semantic is None and _MARKETING.search(local_prefix):
                semantic = "discount"
            explicit = bool(token.group("unit") or token.group("currency") or token.group("sign"))
            if "." not in raw_number and not explicit and semantic != "actual":
                continue
            # Route distances, timestamps, dates and identifiers are not money.
            if re.match(r"\s*(?:公里|千米|km|分钟|小时|分|秒|%|折|积分|年|月|日|:|：|起|件|英寸|寸|[GMKT]B|[GM]Hz|Gbps|mm|cm)", after, re.I):
                continue
            if not (token.group("unit") or token.group("currency")) and (
                    re.search(r"[A-Za-z]$", before) or re.match(r"[A-Za-z]", after)):
                # Decimal model/version strings such as SATA3.0/USB3.2 are not
                # prices even when a product name appears beside a real price.
                continue
            if (before.endswith((":", "：", "-", "/")) and not token.group("sign")
                    and not token.group("unit") and not token.group("currency") and semantic != "actual"):
                continue
            if re.search(r"订单号|订单编号|交易号|流水号|电话|车牌", before[-10:]):
                continue
            try:
                cents = int((Decimal(raw_number) * 100).quantize(Decimal("1")))
            except (InvalidOperation, ValueError):
                continue
            if not 0 < cents <= 100_000_000:
                continue
            score = 1.8 * line.score + min(5.0, 2.0 * line.height / max(median_height, 0.008))
            if explicit:
                score += 0.6
            if semantic == "actual":
                score += 7.0
            if semantic == "discount":
                score -= 12.0
            if semantic == "breakdown":
                score -= 6.0
            if token.group("sign") and kind == "payment":
                score += 3.0
            elif token.group("sign"):
                score -= 3.0
            if semantic == "balance":
                score -= 6.0
            if successes:
                distance = min(abs(line.cy - success.cy) for success in successes)
                if kind == "payment" and distance < 0.17:
                    score += 3.5
                if kind == "order" and any(0 <= line.cy - success.cy < 0.32 for success in successes):
                    score += 1.8
            candidates.append({
                "amount_cents": cents,
                "text": line.text,
                "score": round(max(0.0, min(1.0, score / 14.0)), 3),
                "_score": score,
                "_line": line,
            })
    candidates.sort(key=lambda candidate: candidate["_score"], reverse=True)
    best = candidates[0] if candidates else None
    primary: int | None = None
    primary_line: _Line | None = None
    if best and best["_score"] >= 3.5:
        competing = next((c for c in candidates if c["amount_cents"] != best["amount_cents"]), None)
        if competing and best["_score"] - competing["_score"] < 1.2:
            warnings.append("识别到多个接近的金额，请确认实际支付金额")
        elif best["_line"].score < 0.8:
            warnings.append("实际支付金额的数字识别可信度较低，请手动确认")
        else:
            primary, primary_line = best["amount_cents"], best["_line"]
    if primary is None and not warnings:
        warnings.append("未能可靠识别实际支付金额，请手动填写")
    public_candidates = [{k: v for k, v in candidate.items() if not k.startswith("_")} for candidate in candidates]
    return primary, public_candidates, warnings, primary_line


def _label_value(line: _Line, lines: list[_Line]) -> str:
    """Read a nearby value when OCR separated a label and its value into boxes."""
    same_row = [other for other in lines if other is not line
                and other.left >= line.left + max(0.06, (line.right - line.left) * 0.3)
                and abs(other.cy - line.cy) <= max(line.height, other.height) * 0.8]
    if same_row:
        return line.text + " " + " ".join(other.text for other in sorted(same_row, key=lambda x: x.left))
    below = [other for other in lines if 0 < other.top - line.bottom < 0.035
             and other.left >= line.left - 0.05]
    return line.text + " " + " ".join(other.text for other in below[:2])


def _date_time(lines: list[_Line], kind: str) -> tuple[str | None, str | None]:
    # Never use the phone's screenshot clock. Only transaction/route labels count.
    values: list[tuple[int, str]] = []
    for line in lines:
        if line.top < 0.055:
            continue
        text = _compact(line.text)
        if re.search(r"(?:支付|付款|扣款|交易)(?:时间|日期)", text):
            values.append((0 if kind == "payment" else 2, _label_value(line, lines)))
        elif re.search(r"(?:订单|下单|乘车|出发|用车|创建)(?:时间|日期)", text):
            values.append((1, _label_value(line, lines)))
        elif kind == "order" and re.search(r"下车|到达时间|行程结束", text):
            values.append((0, _label_value(line, lines)))
        elif kind == "order" and re.search(r"上车", text):
            values.append((3, _label_value(line, lines)))
    date: str | None = None
    time: str | None = None
    for _, text in sorted(values, key=lambda value: value[0]):
        match = _DATE.search(text)
        if match and date is None:
            try:
                date = datetime(*map(int, match.groups())).date().isoformat()
            except ValueError:
                pass
        # OCR often removes the space in "2026-10-09 13:25:49". Removing the
        # date first prevents treating the minutes (25:49) as the hour/minute.
        match = _TIME.search(_DATE.sub(" ", text))
        if match and time is None and int(match.group(1)) <= 23:
            time = f"{int(match.group(1)):02d}:{match.group(2)}"
            if match.group(3):
                time += f":{match.group(3)}"
    return date, time


def _merchant(lines: list[_Line]) -> str:
    text = "\n".join(_compact(line.text) for line in lines)
    for aliases, merchant in [
        (("高德打车", "高德地图", "高德优惠券"), "高德打车"),
        (("滴滴出行", "滴滴快车", "滴滴专车", "滴滴打车"), "滴滴出行"),
        (("美团打车",), "美团打车"),
        (("铁路12306", "中国铁路",), "中国铁路"),
    ]:
        if any(alias in text for alias in aliases):
            return merchant
    for line in lines:
        if re.fullmatch(r".{2,40}平台商户", _compact(line.text)):
            return _compact(line.text)
        if re.search(r"商户名称|收款方|商家名称", line.text):
            value = _compact(_label_value(line, lines))
            value = re.sub(r"^.*?(?:商户名称|收款方|商家名称)[:：]?", "", value)
            if value:
                return value[:80]
    return ""


def _order_number(lines: list[_Line]) -> str | None:
    for line in lines:
        if not re.search(r"(?:商户)?订单(?:编号|号)", _compact(line.text)):
            continue
        value = _compact(_label_value(line, lines))
        match = re.search(r"(?:商户)?订单(?:编号|号)[:：]?([A-Za-z0-9_-]{6,64})", value)
        if match:
            return match.group(1)
    return None


def parse_ocr_result(raw: list, width: int, height: int) -> dict:
    """Parse RapidOCR boxes into receipt fields."""
    lines = _normalise_lines(raw, width, height)
    kind = _classify(lines)
    amount, candidates, warnings, amount_line = _amounts(lines, kind)
    date, time = _date_time(lines, kind)
    crop = [0.0, 0.0, 1.0, 1.0]
    if kind == "order" and amount_line is not None:
        promo = [line.top for line in lines if _PROMO.search(line.text)
                 and line.top > max(0.45, amount_line.bottom + 0.06)]
        if promo:
            crop[3] = round(max(amount_line.bottom + 0.035, min(promo) - 0.012), 4)
    if kind == "unknown":
        warnings.append("截图类型不确定，请确认是订单还是扣款凭证")
    if not lines:
        warnings.append("图片中没有识别到文字")
    average = sum(line.score for line in lines) / max(1, len(lines))
    if amount_line is not None:
        average = min(average, amount_line.score)
    confidence = average * (0.5 + 0.25 * (kind != "unknown") + 0.25 * (amount is not None))
    return {
        "kind": kind,
        "amount_cents": amount,
        "amount_candidates": candidates,
        "date": date,
        "time": time,
        "merchant": _merchant(lines),
        "order_no": _order_number(lines),
        "confidence": round(confidence, 3),
        "text": "\n".join(line.text for line in lines),
        "crop": crop,
        "warnings": warnings,
    }


def recognize_image(path: Path) -> dict:
    """Recognize one local screenshot. Image content never leaves this process."""
    from PIL import Image, ImageOps
    import numpy as np

    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        width, height = image.size
        if width < 20 or height < 20:
            raise ValueError("图片尺寸过小，无法识别")
        # RapidOCR accepts RGB PIL images through its loader. Using an ndarray in
        # BGR order keeps the contract explicit and handles EXIF before OCR.
        array = np.asarray(image)[:, :, ::-1].copy()
    engine = _get_engine()
    with _inference_lock:
        output = engine(array)
    boxes = output.boxes if output.boxes is not None else []
    result = [[box.tolist(), text, float(score)]
              for box, text, score in zip(boxes, output.txts or [], output.scores or [])]
    return parse_ocr_result(result, width, height)


def _seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parts = [int(part) for part in value.split(":")]
        if len(parts) not in (2, 3) or not 0 <= parts[0] <= 23 or not 0 <= parts[1] <= 59:
            return None
        return parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) == 3 else 0)
    except (ValueError, AttributeError):
        return None


def _compatible(order: dict, payment: dict) -> bool:
    if order.get("order_no") and payment.get("order_no"):
        if _compact(str(order["order_no"])) != _compact(str(payment["order_no"])):
            return False
        return True
    if order.get("date") and payment.get("date") and order["date"] != payment["date"]:
        return False
    a, b = order.get("merchant", ""), payment.get("merchant", "")
    if a and b and _compact(a) not in _compact(b) and _compact(b) not in _compact(a):
        return False
    a_time, b_time = _seconds(order.get("time")), _seconds(payment.get("time"))
    if a_time is not None and b_time is not None:
        distance = abs(a_time - b_time)
        # A screenshot timestamp is never extracted; these are labelled trip or
        # payment times. A large difference is a reason to request review.
        if min(distance, 86400 - distance) > 4 * 3600:
            return False
    return True


def _reliable(record: dict) -> bool:
    if record.get("manually_edited"):
        return True
    confidence = record.get("confidence")
    return confidence is None or (isinstance(confidence, (int, float)) and confidence >= 0.75)


def _eligible_payments(order: dict, payments: list[dict]) -> list[dict]:
    amount = order.get("amount_cents")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0 or not _reliable(order):
        return []
    pool = [payment for payment in payments
            if isinstance(payment.get("amount_cents"), int)
            and not isinstance(payment.get("amount_cents"), bool)
            and _reliable(payment)
            and 0 < payment["amount_cents"] <= amount
            and _compatible(order, payment)]
    exact_order = [payment for payment in pool if order.get("order_no")
                   and payment.get("order_no") == order["order_no"]]
    # Explicit shared merchant order numbers are stronger than a coincidental
    # amount. Missing numbers cannot override those positively identified bills.
    if exact_order:
        pool = exact_order
    return sorted(pool, key=lambda payment: (payment["amount_cents"], str(payment["id"])))


def _candidate_sets(order: dict, payments: list[dict]) -> tuple[list[tuple[str, ...]], bool]:
    pool = _eligible_payments(order, payments)
    if not pool:
        return [], False
    amount = order["amount_cents"]
    found: list[tuple[str, ...]] = []
    visited = 0

    class SearchLimit(Exception):
        pass

    def append_result(subset: list[dict]) -> None:
        found.append(tuple(sorted(str(payment["id"]) for payment in subset)))
        if len(found) > 128:
            raise SearchLimit

    # Four payments per order are supported. Sorting and minimum/maximum sums
    # eliminate entire branches; cents GCD also cheaply rules out impossible
    # splits. This allows hundreds of screenshots without a small pool cap.
    for count in range(1, min(4, len(pool)) + 1):
        search_pool = pool if count == 1 else [payment for payment in pool if payment["amount_cents"] < amount]
        values = [payment["amount_cents"] for payment in search_pool]
        length = len(values)
        if length < count or amount % reduce(gcd, values, 0):
            continue
        prefix = [0]
        for value in values:
            prefix.append(prefix[-1] + value)
        suffix_gcd = [0] * (length + 1)
        for index in range(length - 1, -1, -1):
            suffix_gcd[index] = gcd(values[index], suffix_gcd[index + 1])

        def search(start: int, remaining: int, slots: int, subset: list[dict]) -> None:
            nonlocal visited
            visited += 1
            if visited > 20_000:
                raise SearchLimit
            if length - start < slots:
                return
            if prefix[start + slots] - prefix[start] > remaining:
                return
            if prefix[length] - prefix[length - slots] < remaining:
                return
            if remaining % suffix_gcd[start]:
                return
            if slots == 1:
                lower = bisect_left(values, remaining, start)
                upper = bisect_right(values, remaining, lower)
                for index in range(lower, upper):
                    append_result(subset + [search_pool[index]])
                return
            maximum_rest = prefix[length] - prefix[length - slots + 1]
            for index in range(start, length - slots + 1):
                if prefix[index + slots] - prefix[index] > remaining:
                    break
                if values[index] + maximum_rest < remaining:
                    continue
                search(index + 1, remaining - values[index], slots - 1, subset + [search_pool[index]])

        try:
            search(0, amount, count, [])
        except SearchLimit:
            return [], True
    return found, False


def match_records(records: list[dict]) -> dict:
    """Match only unique exact-sum sets, leaving every ambiguous image reviewable.

    Every order gets a group. Review groups have no assigned payments. Loose
    payments and unknown images are returned in ``unmatched_ids``. A payment is
    never consumed by more than one group. Splits are bounded to four images.
    """
    orders = sorted((record for record in records if record.get("kind") == "order"),
                    key=lambda record: (record.get("date") or "9999", record.get("time") or "99", str(record["id"])))
    payments = [record for record in records if record.get("kind") == "payment"]
    options: dict[str, list[tuple[str, ...]]] = {}
    bounded: set[str] = set()
    for order in orders:
        ident = str(order["id"])
        options[ident], limit = _candidate_sets(order, payments)
        if limit:
            bounded.add(ident)
    # Any candidate involving a payment claimed by another order is ambiguous,
    # even when a greedy traversal would have happened to pick one first.
    owners: dict[str, set[str]] = {}
    for ident, alternatives in options.items():
        for subset in alternatives:
            for payment_id in subset:
                owners.setdefault(payment_id, set()).add(ident)
    for order in orders:
        ident = str(order["id"])
        if ident in bounded:
            # A bounded search may conceal a competing set. Only potentially
            # shared payments block another group; unrelated dates, merchants,
            # order numbers and amounts retain their independent matches.
            for payment in _eligible_payments(order, payments):
                owners.setdefault(str(payment["id"]), set()).add(ident)
    groups: list[dict] = []
    assigned: set[str] = set()
    for order in orders:
        ident = str(order["id"])
        alternatives = options[ident]
        group = {"id": f"group-{ident}", "order_id": ident, "payment_ids": [], "status": "review", "reason": ""}
        if ident in bounded:
            group["reason"] = "候选截图较多，自动匹配范围不足，请手动确认"
        elif order.get("amount_cents") is None:
            group["reason"] = "订单金额需要确认"
        elif not _reliable(order):
            group["reason"] = "订单文字识别可信度较低，请手动确认"
        elif not alternatives:
            group["reason"] = "未找到金额及已识别信息一致的扣款截图"
        elif len(alternatives) != 1:
            group["reason"] = "存在多种金额相同的扣款组合，请手动确认"
        elif any(len(owners[payment_id]) != 1 for payment_id in alternatives[0]):
            group["reason"] = "同一扣款截图可能对应多个订单，请手动确认"
        else:
            group["payment_ids"] = list(alternatives[0])
            group["status"] = "matched"
            count = len(alternatives[0])
            group["reason"] = "金额一致，且没有其他候选组合" if count == 1 else f"{count} 张扣款金额合计与订单一致，且组合唯一"
            assigned.update(alternatives[0])
        assigned.add(ident)
        groups.append(group)
    return {"groups": groups, "unmatched_ids": [str(record["id"]) for record in records if str(record["id"]) not in assigned]}
