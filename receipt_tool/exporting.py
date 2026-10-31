"""Build Word (.docx) and PDF (.pdf) from the same A4 layout using only Python stdlib and Pillow.

Eliminates python-docx, lxml, reportlab, and charset-normalizer (~10 MiB total)
while producing standard-compliant OpenXML (.docx) and PDF 1.4 documents.
"""
from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
from zipfile import ZIP_DEFLATED, ZipFile
import zlib

from PIL import Image

MM = 72 / 25.4
A4 = (210 * MM, 297 * MM)

# Exact character widths (1/1000 em) for ASCII 32..126 in Adobe STSong-Light CIDFont.
_STSONG_ASCII_WIDTHS = (
    207, 270, 342, 467, 462, 797, 710, 239, 374, 374, 423, 605, 238, 375, 238, 334,
    462, 462, 462, 462, 462, 462, 462, 462, 462, 462, 238, 238, 605, 605, 605, 344,
    748, 684, 560, 695, 739, 563, 511, 729, 793, 318, 312, 666, 526, 896, 758, 772,
    544, 772, 628, 465, 607, 753, 711, 972, 647, 620, 607, 374, 333, 374, 606, 500,
    239, 417, 503, 427, 529, 415, 264, 444, 518, 241, 230, 495, 228, 793, 527, 524,
    524, 504, 338, 336, 277, 517, 450, 652, 466, 452, 407, 370, 258, 370, 605,
)


def _string_width(text: str, font_size: float) -> float:
    units = sum(_STSONG_ASCII_WIDTHS[ord(ch) - 32] if 32 <= ord(ch) <= 126 else 1000 for ch in text)
    return units * font_size / 1000.0


def _heading(heading: str, title: str) -> tuple[str, float]:
    text = " · ".join(value for value in [title, heading] if value)
    width = _string_width(text, 10.0)
    return text, min(10.0, 10.0 * 186.0 * MM / max(1.0, width))


def _image_bytes(path: Path, crop: list | None) -> tuple[bytes, int, int]:
    with Image.open(path) as source:
        image = source.convert("RGB")
        if crop:
            left, top, right, bottom = crop
            image = image.crop((
                round(left * image.width),
                round(top * image.height),
                round(right * image.width),
                round(bottom * image.height),
            ))
        buf = BytesIO()
        image.save(buf, "PNG", optimize=True)
        return buf.getvalue(), image.width, image.height


def _pages(batch: dict, directory: Path, use_crop: bool, include_incomplete: bool):
    records = {r["id"]: r for r in batch["records"]}
    used = set()
    pages = []
    for group in batch["groups"]:
        ids = [group["order_id"], *group["payment_ids"]]
        used.update(ids)
        chunks = [group["payment_ids"][i : i + 3] for i in range(0, len(group["payment_ids"]), 3)] or [[]]
        for part, payments in enumerate(chunks):
            heading = "待核对" if group.get("status") != "matched" else ""
            if len(chunks) > 1:
                heading = f"{heading + ' · ' if heading else ''}第 {part + 1}/{len(chunks)} 页"
            images = [
                _image_bytes(directory / records[rid]["file"], records[rid].get("crop") if use_crop else None)
                for rid in [group["order_id"], *payments]
            ]
            pages.append((heading, images))
    if include_incomplete:
        for record in batch["records"]:
            if record["id"] not in used:
                pages.append(("待核对 · 未配对截图", [_image_bytes(directory / record["file"], None)]))
    return pages


def _layout(images, heading: str, title: str):
    gap = 2 * MM
    available_height = (260 if heading or title else 270) * MM
    width = min(
        116 * MM,
        (available_height - gap * (len(images) - 1)) / sum(height / width for _, width, height in images),
    )
    return [(data, width, width * height / original_width) for data, original_width, height in images], gap


def _docx(pages, title: str) -> bytes:
    media: list[tuple[str, bytes]] = []
    media_by_hash: dict[str, str] = {}
    paragraphs: list[str] = []
    pic_id = 0

    for page_index, (heading, images) in enumerate(pages):
        planned, gap = _layout(images, heading, title)
        if title or heading:
            text, font_size = _heading(heading, title)
            sz_half_pt = max(2, round(font_size * 2))
            pb = '<w:pageBreakBefore/>' if page_index else '<w:pageBreakBefore w:val="0"/>'
            paragraphs.append(
                f'<w:p><w:pPr>{pb}<w:spacing w:before="0" w:after="100"/><w:jc w:val="center"/></w:pPr>'
                f'<w:r><w:rPr><w:rFonts w:ascii="宋体" w:hAnsi="宋体" w:eastAsia="宋体" w:cs="宋体"/>'
                f'<w:sz w:val="{sz_half_pt}"/><w:szCs w:val="{sz_half_pt}"/></w:rPr>'
                f'<w:t>{xml_escape(text)}</w:t></w:r></w:p>'
            )
        for i, (data, width, height) in enumerate(planned):
            digest = hashlib.sha256(data).hexdigest()
            rel_id = media_by_hash.get(digest)
            if rel_id is None:
                idx = len(media) + 1
                rel_id = f"rIdImg{idx}"
                media_by_hash[digest] = rel_id
                media.append((f"image{idx}.png", data))
            pic_id += 1
            cx = max(1, round(width * 12700))
            cy = max(1, round(height * 12700))
            after_twips = round((gap if i < len(planned) - 1 else 0) * 20)
            pb = ""
            if not title and not heading and i == 0:
                pb = '<w:pageBreakBefore/>' if page_index else '<w:pageBreakBefore w:val="0"/>'
            paragraphs.append(
                f'<w:p><w:pPr>{pb}<w:spacing w:before="0" w:after="{after_twips}"/><w:jc w:val="center"/></w:pPr>'
                f'<w:r><w:drawing>'
                f'<wp:inline distT="0" distB="0" distL="0" distR="0" '
                f'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                f'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
                f'<wp:extent cx="{cx}" cy="{cy}"/>'
                f'<wp:effectExtent l="0" t="0" r="0" b="0"/>'
                f'<wp:docPr id="{pic_id}" name="Picture {pic_id}"/>'
                f'<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
                f'<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
                f'<pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="image.png"/><pic:cNvPicPr/></pic:nvPicPr>'
                f'<pic:blipFill><a:blip r:embed="{rel_id}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
                f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
                f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
                f'</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
            )

    doc_title = xml_escape(title or "订单与支付凭证")
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="png" ContentType="image/png"/>'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        '</Relationships>'
    )
    core_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f'<dc:title>{doc_title}</dc:title><dc:creator>凭证归档</dc:creator>'
        '<cp:revision>1</cp:revision></cp:coreProperties>'
    )
    app_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        '<Application>凭证归档</Application></Properties>'
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/>'
        '<w:pPr><w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
        '<w:rPr><w:rFonts w:ascii="宋体" w:hAnsi="宋体" w:eastAsia="宋体" w:cs="宋体"/>'
        '<w:sz w:val="2"/><w:szCs w:val="2"/></w:rPr>'
        '</w:style></w:styles>'
    )
    rel_items = [
        '<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    ]
    for idx, (filename, _) in enumerate(media, start=1):
        rel_items.append(
            f'<Relationship Id="rIdImg{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/{filename}"/>'
        )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(rel_items)
        + '</Relationships>'
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body>'
        + "".join(paragraphs)
        + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="680" w:right="680" w:bottom="680" w:left="680" w:header="720" w:footer="720" w:gutter="0"/>'
        '</w:sectPr></w:body></w:document>'
    )

    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("docProps/core.xml", core_xml)
        archive.writestr("docProps/app.xml", app_xml)
        archive.writestr("word/styles.xml", styles_xml)
        archive.writestr("word/_rels/document.xml.rels", doc_rels)
        archive.writestr("word/document.xml", document_xml)
        for filename, raw_bytes in media:
            archive.writestr(f"word/media/{filename}", raw_bytes)
    return output.getvalue()


def _pdf_utf16_hex(value: str) -> str:
    return "<FEFF" + value.encode("utf-16-be").hex().upper() + ">"


def _pdf(pages, title: str) -> bytes:
    objects: list[bytes] = []

    def add_object(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    # Reserve 1: Catalog, 2: Pages, 3: Font, 4: Info
    for _ in range(4):
        add_object(b"")

    w_entries = " ".join(str(w) for w in _STSONG_ASCII_WIDTHS)
    objects[2] = (
        b"<< /Type /Font /Subtype /Type0 /Name /F1 /BaseFont /STSong-Light /Encoding /UniGB-UCS2-H "
        b"/DescendantFonts [ << /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 0 >> /DW 1000 "
        b"/FontDescriptor << /Type /FontDescriptor /FontName /STSongStd-Light /Flags 6 "
        b"/FontBBox [ -25 -254 1000 880 ] /ItalicAngle 0 /Ascent 752 /Descent -271 "
        b"/CapHeight 737 /StemV 58 >> "
        b"/W [ 1 [ " + w_entries.encode("ascii") + b" ] ] >> ] >>"
    )
    doc_title = _pdf_utf16_hex(title or "订单与支付凭证").encode("ascii")
    doc_author = _pdf_utf16_hex("凭证归档").encode("ascii")
    objects[3] = (
        b"<< /Title " + doc_title + b" /Author " + doc_author + b" /Producer (Receipt Archive) >>"
    )

    image_obj_by_hash: dict[str, tuple[int, str]] = {}
    page_obj_ids: list[int] = []

    for heading, images in pages:
        planned, gap = _layout(images, heading, title)
        top = A4[1] - 12 * MM
        ops: list[str] = []
        page_xobjects: dict[str, int] = {}

        if title or heading:
            text, font_size = _heading(heading, title)
            text_w = _string_width(text, font_size)
            x = (A4[0] - text_w) / 2.0
            y = top - 10.0
            hex_text = "<" + text.encode("utf-16-be").hex().upper() + ">"
            ops.append(f"BT /F1 {font_size:.4f} Tf 1 0 0 1 {x:.4f} {y:.4f} Tm {hex_text} Tj ET")
            top -= 20.0

        for data, width, height in planned:
            digest = hashlib.sha256(data).hexdigest()
            cached = image_obj_by_hash.get(digest)
            if cached is None:
                with Image.open(BytesIO(data)) as img:
                    rgb = img.convert("RGB")
                    iw, ih = rgb.size
                    compressed = zlib.compress(rgb.tobytes(), level=6)
                im_name = f"Im{len(image_obj_by_hash) + 1}"
                im_obj = add_object(
                    f"<< /Type /XObject /Subtype /Image /Width {iw} /Height {ih} "
                    f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
                    f"/Length {len(compressed)} >>\nstream\n".encode("ascii")
                    + compressed
                    + b"\nendstream"
                )
                cached = (im_obj, im_name)
                image_obj_by_hash[digest] = cached
            im_obj, im_name = cached
            page_xobjects[im_name] = im_obj
            x_img = (A4[0] - width) / 2.0
            y_img = top - height
            ops.append(f"q {width:.4f} 0 0 {height:.4f} {x_img:.4f} {y_img:.4f} cm /{im_name} Do Q")
            top -= height + gap

        stream_bytes = zlib.compress("\n".join(ops).encode("ascii"), level=6)
        content_id = add_object(
            f"<< /Filter /FlateDecode /Length {len(stream_bytes)} >>\nstream\n".encode("ascii")
            + stream_bytes
            + b"\nendstream"
        )
        xobj_dict = " ".join(f"/{name} {oid} 0 R" for name, oid in page_xobjects.items())
        page_id = add_object(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [ 0 0 {A4[0]:.4f} {A4[1]:.4f} ] "
            f"/Resources << /Font << /F1 3 0 R >> /XObject << {xobj_dict} >> >> "
            f"/Contents {content_id} 0 R >>".encode("ascii")
        )
        page_obj_ids.append(page_id)

    kids = " ".join(f"{pid} 0 R" for pid in page_obj_ids)
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Count {len(page_obj_ids)} /Kids [ {kids} ] >>".encode("ascii")

    buf = BytesIO()
    buf.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for idx, body in enumerate(objects, start=1):
        offsets.append(buf.tell())
        buf.write(f"{idx} 0 obj\n".encode("ascii"))
        buf.write(body)
        buf.write(b"\nendobj\n")
    xref_pos = buf.tell()
    buf.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        buf.write(f"{offset:010d} 00000 n \n".encode("ascii"))
    buf.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 4 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return buf.getvalue()


def export_documents(
    batch: dict,
    directory: Path,
    fmt: str = "zip",
    crop: bool = True,
    title: str = "",
    allow_incomplete: bool = False,
) -> tuple[bytes, str, str]:
    pages = _pages(batch, directory, crop, allow_incomplete)
    if not pages:
        raise ValueError("没有可导出的截图，请先配对订单与支付凭证。")
    if fmt == "docx":
        return _docx(pages, title), "凭证归档.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if fmt == "pdf":
        return _pdf(pages, title), "凭证归档.pdf", "application/pdf"
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("凭证归档.docx", _docx(pages, title))
        archive.writestr("凭证归档.pdf", _pdf(pages, title))
    return output.getvalue(), "凭证归档_Word和PDF.zip", "application/zip"
