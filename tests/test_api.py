import json
import os
import shutil
import tempfile
import unittest
from io import BytesIO

from PIL import Image

_DATA_DIR = tempfile.mkdtemp(prefix="receipt-api-test-")
os.environ["RECEIPT_DATA_DIR"] = _DATA_DIR

from receipt_tool import app as receipt_app  # noqa: E402  (environment must be set first)


def png_bytes():
    output = BytesIO()
    Image.new("RGB", (120, 240), "white").save(output, "PNG")
    return output.getvalue()


def multipart(files, boundary="----receipt-test-boundary"):
    chunks = []
    for filename, payload in files:
        chunks.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
            "Content-Type: image/png\r\n\r\n".encode("ascii")
        )
        chunks.append(payload)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.png = png_bytes()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_DATA_DIR, ignore_errors=True)

    def dispatch(self, method, path, body=b"", headers=None):
        base = {"host": "127.0.0.1"}
        base.update(headers or {})
        status, response_headers, payload = receipt_app._dispatch(method, path, base, body)
        return status, response_headers, payload

    def post_images(self, files):
        body, content_type = multipart(files)
        return self.dispatch("POST", "/api/batches", body, {"content-type": content_type})

    def test_health_is_local_only(self):
        status, headers, payload = self.dispatch("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        health = json.loads(payload)
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["local_only"])
        self.assertEqual(health["max_files"], 500)

    def test_foreign_host_header_is_rejected(self):
        status, _, payload = self.dispatch("GET", "/api/health", headers={"host": "example.com"})
        self.assertEqual(status, 400)
        self.assertIn(b"Invalid host header", payload)

    def test_index_and_static_files_are_served(self):
        for path in ("/", "/static/app.js", "/static/style.css"):
            status, _, payload = self.dispatch("GET", path)
            self.assertEqual(status, 200, path)
            self.assertTrue(payload)

    def test_path_traversal_is_blocked(self):
        status, _, _ = self.dispatch("GET", "/static/../receipt_tool/app.py")
        self.assertEqual(status, 404)

    def test_unknown_api_route_returns_404(self):
        status, _, payload = self.dispatch("GET", "/api/does-not-exist")
        self.assertEqual(status, 404)
        self.assertIn("接口不存在", json.loads(payload)["detail"])

    def test_duplicate_images_are_counted_but_not_stored_twice(self):
        status, _, payload = self.post_images([("a.png", self.png), ("b.png", self.png)])
        self.assertEqual(status, 200)
        batch = json.loads(payload)
        self.assertEqual(len(batch["records"]), 1)
        self.assertEqual(len(batch["duplicates"]), 1)
        self.assertEqual(batch["duplicates"][0]["duplicate_of"], batch["records"][0]["id"])
        self.assertEqual(batch["total"], 1)

        status, _, latest = self.dispatch("GET", "/api/batches/latest")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(latest)["id"], batch["id"])

        status, _, deleted = self.dispatch("DELETE", f"/api/batches/{batch['id']}")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(deleted)["deleted"])

    def test_invalid_upload_shape_is_rejected(self):
        status, _, payload = self.dispatch(
            "POST", "/api/batches", b"not-an-image", {"content-type": "multipart/form-data; boundary=x"}
        )
        self.assertEqual(status, 422)
        self.assertIn("detail", json.loads(payload))

    def test_export_options_are_validated(self):
        with self.assertRaises(receipt_app.HTTPException) as raised:
            receipt_app._validate_export_options({"format": "exe"})
        self.assertEqual(raised.exception.status_code, 422)

        options = receipt_app._validate_export_options({"format": "pdf", "crop": False, "title": " 十月 "})
        self.assertEqual(options, {"format": "pdf", "crop": False, "title": "十月", "allow_incomplete": False})

    def test_record_edits_reject_bad_amounts(self):
        for amount in (0, -1, 100_000_001, True, "19.90", 19.9):
            with self.assertRaises(receipt_app.HTTPException, msg=repr(amount)):
                receipt_app._validate_record_edit({"amount_cents": amount})

        with self.assertRaises(receipt_app.HTTPException):
            receipt_app._validate_record_edit({"unknown": 1})

        self.assertEqual(receipt_app._validate_record_edit({"amount_cents": 1990}), {"amount_cents": 1990})


if __name__ == "__main__":
    unittest.main()
