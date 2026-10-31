import unittest

from receipt_tool.recognition import match_records, parse_ocr_result

WIDTH, HEIGHT = 1000, 2000


def line(text, y, x=0.10, score=0.96):
    """Build one OCR box in pixel coordinates, like the engine returns."""
    width = min(0.80, 0.045 * len(text))
    return [
        [[x * WIDTH, y * HEIGHT], [(x + width) * WIDTH, y * HEIGHT],
         [(x + width) * WIDTH, (y + 0.03) * HEIGHT], [x * WIDTH, (y + 0.03) * HEIGHT]],
        text,
        score,
    ]


def record(ident, kind, cents, *, date="2026-10-09", merchant="滴滴出行",
           order_no=None, confidence=0.95, edited=False):
    return {
        "id": ident,
        "kind": kind,
        "amount_cents": cents,
        "date": date,
        "merchant": merchant,
        "order_no": order_no,
        "confidence": confidence,
        "manually_edited": edited,
    }


class ParseReceiptTest(unittest.TestCase):
    def parse(self, rows):
        return parse_ocr_result([line(text, y, score=score) for text, y, score in rows], WIDTH, HEIGHT)

    def test_payment_screenshot(self):
        result = self.parse([
            ("账单详情", 0.08, 0.96),
            ("自动扣款成功", 0.13, 0.96),
            ("扣款金额 ¥19.90", 0.30, 0.96),
            ("扣款时间 2026-10-09 13:25", 0.40, 0.96),
        ])
        self.assertEqual(result["kind"], "payment")
        self.assertEqual(result["amount_cents"], 1990)
        self.assertEqual(result["date"], "2026-10-09")
        self.assertEqual(result["time"], "13:25")

    def test_order_screenshot_with_order_number(self):
        result = self.parse([
            ("订单详情", 0.08, 0.96),
            ("实付款 ¥19.90", 0.30, 0.96),
            ("订单号 2026100912345678", 0.40, 0.96),
        ])
        self.assertEqual(result["kind"], "order")
        self.assertEqual(result["amount_cents"], 1990)
        self.assertEqual(result["order_no"], "2026100912345678")

    def test_refund_is_never_classified_as_spending(self):
        result = self.parse([
            ("微信支付", 0.08, 0.96),
            ("退款成功", 0.13, 0.96),
            ("退款金额 ¥19.90", 0.30, 0.96),
        ])
        self.assertEqual(result["kind"], "unknown")
        self.assertIn("截图类型不确定，请确认是订单还是扣款凭证", result["warnings"])

    def test_actual_amount_beats_breakdown_and_discount(self):
        result = self.parse([
            ("支付成功", 0.08, 0.96),
            ("商品金额 ¥25.90", 0.30, 0.96),
            ("优惠 -¥6.00", 0.36, 0.96),
            ("实付款 ¥19.90", 0.50, 0.96),
        ])
        self.assertEqual(result["amount_cents"], 1990)
        candidates = {item["amount_cents"] for item in result["amount_candidates"]}
        self.assertTrue({1990, 2590, 600}.issubset(candidates))

    def test_merchant_alias_and_order_number(self):
        result = self.parse([
            ("支付成功", 0.08, 0.96),
            ("实付 ¥8.00", 0.30, 0.96),
            ("商户名称：滴滴出行", 0.40, 0.96),
            ("订单编号 202610090001", 0.50, 0.96),
        ])
        self.assertEqual(result["merchant"], "滴滴出行")
        self.assertEqual(result["order_no"], "202610090001")

    def test_empty_image_asks_for_manual_input(self):
        result = parse_ocr_result([], WIDTH, HEIGHT)
        self.assertEqual(result["kind"], "unknown")
        self.assertIsNone(result["amount_cents"])
        self.assertIn("图片中没有识别到文字", result["warnings"])

    def test_merged_date_and_time_is_not_read_as_minutes(self):
        result = self.parse([
            ("支付成功", 0.08, 0.96),
            ("实付 ¥12.00", 0.30, 0.96),
            ("支付时间 2026-10-09 13:25:49", 0.40, 0.96),
        ])
        self.assertEqual(result["date"], "2026-10-09")
        self.assertEqual(result["time"], "13:25:49")


class MatchingTest(unittest.TestCase):
    def test_exact_amount_and_merchant_matches(self):
        result = match_records([record("o1", "order", 2000), record("p1", "payment", 2000)])
        self.assertEqual(result["groups"][0]["status"], "matched")
        self.assertEqual(result["groups"][0]["payment_ids"], ["p1"])
        self.assertEqual(result["unmatched_ids"], [])

    def test_split_payments_match_one_order(self):
        result = match_records([
            record("o1", "order", 2000),
            record("p1", "payment", 1200),
            record("p2", "payment", 800),
        ])
        self.assertEqual(result["groups"][0]["status"], "matched")
        self.assertEqual(result["groups"][0]["payment_ids"], ["p1", "p2"])

    def test_ambiguous_subsets_require_review(self):
        result = match_records([
            record("o1", "order", 1000),
            record("p1", "payment", 1000),
            record("p2", "payment", 500),
            record("p3", "payment", 500),
        ])
        group = result["groups"][0]
        self.assertEqual(group["status"], "review")
        self.assertEqual(group["payment_ids"], [])
        self.assertIn("多种金额相同的扣款组合", group["reason"])

    def test_payment_cannot_be_claimed_by_two_orders(self):
        result = match_records([
            record("o1", "order", 1000),
            record("o2", "order", 1000),
            record("p1", "payment", 1000),
        ])
        self.assertEqual([group["status"] for group in result["groups"]], ["review", "review"])
        self.assertEqual(result["unmatched_ids"], ["p1"])

    def test_different_date_blocks_match(self):
        result = match_records([
            record("o1", "order", 1000),
            record("p1", "payment", 1000, date="2026-10-10"),
        ])
        self.assertEqual(result["groups"][0]["status"], "review")

    def test_conflicting_order_numbers_never_match(self):
        result = match_records([
            record("o1", "order", 1000, order_no="A1"),
            record("p1", "payment", 1000, order_no="B2"),
        ])
        self.assertEqual(result["groups"][0]["status"], "review")

    def test_low_confidence_order_is_left_for_review(self):
        result = match_records([
            record("o1", "order", 1000, confidence=0.40),
            record("p1", "payment", 1000),
        ])
        self.assertEqual(result["groups"][0]["status"], "review")
        self.assertIn("可信度较低", result["groups"][0]["reason"])

    def test_manual_edit_overrides_low_confidence(self):
        result = match_records([
            record("o1", "order", 1000, confidence=0.40, edited=True),
            record("p1", "payment", 1000),
        ])
        self.assertEqual(result["groups"][0]["status"], "matched")


if __name__ == "__main__":
    unittest.main()
