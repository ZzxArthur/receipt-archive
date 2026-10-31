import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from PIL import Image

from receipt_tool.exporting import export_documents


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="receipt-export-test-")
        self.directory = Path(self.temporary.name)
        Image.new("RGB", (300, 600), "white").save(self.directory / "order.png")
        Image.new("RGB", (300, 600), "gray").save(self.directory / "payment.png")
        self.batch = {
            "records": [
                {"id": "o1", "file": "order.png", "kind": "order", "crop": [0, 0, 1, 1]},
                {"id": "p1", "file": "payment.png", "kind": "payment", "crop": [0, 0, 1, 1]},
            ],
            "groups": [
                {"id": "g1", "order_id": "o1", "payment_ids": ["p1"], "status": "matched"},
            ],
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_docx_contains_one_page_with_two_images(self):
        payload, filename, media_type = export_documents(self.batch, self.directory, "docx", True, "")
        self.assertEqual(filename, "凭证归档.docx")
        self.assertEqual(media_type, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            names = archive.namelist()
            self.assertIn("word/media/image1.png", names)
            self.assertIn("word/media/image2.png", names)
            document = archive.read("word/document.xml").decode("utf-8")
        self.assertEqual(document.count("<w:drawing>"), 2)
        self.assertIn('<w:pageBreakBefore w:val="0"/>', document)

    def test_pdf_is_valid_and_has_one_page(self):
        payload, filename, media_type = export_documents(self.batch, self.directory, "pdf", True, "")
        self.assertEqual(filename, "凭证归档.pdf")
        self.assertEqual(media_type, "application/pdf")
        self.assertTrue(payload.startswith(b"%PDF-1.4"))
        self.assertTrue(payload.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"/Type /Page", payload)
        self.assertIn(b"/Count 1", payload)

    def test_zip_contains_both_documents(self):
        payload, filename, media_type = export_documents(self.batch, self.directory, "zip", True, "")
        self.assertEqual(filename, "凭证归档_Word和PDF.zip")
        self.assertEqual(media_type, "application/zip")
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            self.assertEqual(sorted(archive.namelist()), ["凭证归档.docx", "凭证归档.pdf"])

    def test_empty_batch_is_rejected(self):
        with self.assertRaises(ValueError):
            export_documents({"records": [], "groups": []}, self.directory, "docx", True, "")


if __name__ == "__main__":
    unittest.main()
