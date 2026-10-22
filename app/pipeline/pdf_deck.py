"""Assemble a .pdf: one picture per page, the same pages as the .pptx.

Written directly rather than through a PDF library. The slides are already
JPEGs, and PDF can carry a JPEG as it is (DCTDecode), so every page is the
captured image byte for byte -- no second round of compression -- and the
file is little bigger than the images themselves. Anything that is not a
plain RGB or greyscale JPEG is re-encoded once on the way in.

A combined deck gets bookmarks (the PDF outline): one per video, pointing at
its first page, and the file opens with the bookmarks panel showing.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from PIL import Image

from app.pipeline import tunables as T
from app.pipeline.pptx_deck import ASPECT_TOLERANCE, DeckSlide

POINTS_PER_IN = 72
REENCODE_QUALITY = 92


@dataclass
class Bookmark:
    title: str
    page: int    # 0-based


def _text(value: str) -> bytes:
    """A PDF text string: literal when plain ASCII, UTF-16 otherwise."""
    if all(32 <= ord(ch) < 127 for ch in value):
        escaped = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        return f"({escaped})".encode("ascii")
    return b"<FEFF" + value.encode("utf-16-be").hex().upper().encode("ascii") + b">"


def _jpeg(path: Path) -> tuple[bytes, int, int, str]:
    """(JPEG bytes, width, height, colour space) for an image XObject."""
    with Image.open(path) as image:
        width, height = image.size
        if image.format == "JPEG" and image.mode in ("RGB", "L"):
            return path.read_bytes(), width, height, "/DeviceRGB" if image.mode == "RGB" else "/DeviceGray"
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "JPEG", quality=REENCODE_QUALITY)
        return buffer.getvalue(), width, height, "/DeviceRGB"


def _fit(image_w: int, image_h: int, page_w: float, page_h: float) -> tuple[float, float, float, float]:
    """(x, y, width, height) in PDF space (origin bottom left): the image
    filling the page, or centred inside it when its shape differs."""
    image_aspect, page_aspect = image_w / image_h, page_w / page_h
    if abs(image_aspect - page_aspect) / page_aspect <= ASPECT_TOLERANCE:
        return 0.0, 0.0, page_w, page_h
    if image_aspect > page_aspect:
        height = page_w / image_aspect
        return 0.0, (page_h - height) / 2, page_w, height
    width = page_h * image_aspect
    return (page_w - width) / 2, 0.0, width, page_h


class _Writer:
    """Objects are written as they are made; offsets go into the xref."""

    def __init__(self, handle: BinaryIO) -> None:
        self.handle = handle
        self.offsets: dict[int, int] = {}
        self.handle.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

    def write(self, number: int, body: bytes, stream: bytes | None = None) -> None:
        self.offsets[number] = self.handle.tell()
        self.handle.write(f"{number} 0 obj\n".encode("ascii"))
        if stream is None:
            self.handle.write(body + b"\nendobj\n")
            return
        self.handle.write(body + b"\nstream\n")
        self.handle.write(stream)
        self.handle.write(b"\nendstream\nendobj\n")

    def finish(self, count: int, root: int, info: int) -> None:
        start = self.handle.tell()
        self.handle.write(f"xref\n0 {count + 1}\n0000000000 65535 f \n".encode("ascii"))
        for number in range(1, count + 1):
            self.handle.write(f"{self.offsets[number]:010d} 00000 n \n".encode("ascii"))
        self.handle.write(f"trailer\n<< /Size {count + 1} /Root {root} 0 R /Info {info} 0 R >>\n"
                          f"startxref\n{start}\n%%EOF\n".encode("ascii"))


def build_pdf(slides: list[DeckSlide], width_px: int, height_px: int, out_pdf: Path, title: str,
              bookmarks: list[Bookmark] | None = None) -> int:
    if not slides:
        raise ValueError("a PDF needs at least one slide")
    page_w = T.SLIDE_WIDTH_IN * POINTS_PER_IN
    page_h = page_w * height_px / width_px
    bookmarks = [mark for mark in (bookmarks or []) if 0 <= mark.page < len(slides)]

    # Numbering, fixed up front so objects can point forward: catalog, page
    # tree, info, outline root; then three objects per page (page, content,
    # image); then one per bookmark.
    catalog, pages, info, outline = 1, 2, 3, 4
    first_page = 5
    page_numbers = [first_page + 3 * index for index in range(len(slides))]
    first_mark = first_page + 3 * len(slides)
    total = first_mark + len(bookmarks) - 1

    partial = out_pdf.with_suffix(".pdf.part")
    with partial.open("wb") as handle:
        pdf = _Writer(handle)
        catalog_body = f"<< /Type /Catalog /Pages {pages} 0 R".encode("ascii")
        if bookmarks:
            catalog_body += f" /Outlines {outline} 0 R /PageMode /UseOutlines".encode("ascii")
        pdf.write(catalog, catalog_body + b" >>")
        kids = " ".join(f"{number} 0 R" for number in page_numbers)
        pdf.write(pages, f"<< /Type /Pages /Kids [{kids}] /Count {len(slides)} >>".encode("ascii"))
        stamp = datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
        pdf.write(info, b"<< /Title " + _text(title) + b" /Producer (VideoToSlides) /CreationDate "
                  + _text(stamp) + b" >>")
        if bookmarks:
            first, last = first_mark, first_mark + len(bookmarks) - 1
            pdf.write(outline, f"<< /Type /Outlines /First {first} 0 R /Last {last} 0 R /Count {len(bookmarks)} >>"
                      .encode("ascii"))
        else:
            pdf.write(outline, b"<< /Type /Outlines /Count 0 >>")

        for index, item in enumerate(slides):
            page, content, picture = page_numbers[index], page_numbers[index] + 1, page_numbers[index] + 2
            data, image_w, image_h, colours = _jpeg(item.image)
            x, y, w, h = _fit(image_w, image_h, page_w, page_h)
            pdf.write(page, (f"<< /Type /Page /Parent {pages} 0 R /MediaBox [0 0 {page_w:.2f} {page_h:.2f}] "
                             f"/Resources << /XObject << /Im0 {picture} 0 R >> >> /Contents {content} 0 R >>")
                      .encode("ascii"))
            drawing = f"q {w:.2f} 0 0 {h:.2f} {x:.2f} {y:.2f} cm /Im0 Do Q".encode("ascii")
            pdf.write(content, f"<< /Length {len(drawing)} >>".encode("ascii"), drawing)
            pdf.write(picture, (f"<< /Type /XObject /Subtype /Image /Width {image_w} /Height {image_h} "
                                f"/ColorSpace {colours} /BitsPerComponent 8 /Filter /DCTDecode /Length {len(data)} >>")
                      .encode("ascii"), data)

        for offset, mark in enumerate(bookmarks):
            number = first_mark + offset
            links = f" /Parent {outline} 0 R /Dest [{page_numbers[mark.page]} 0 R /Fit]"
            if offset > 0:
                links += f" /Prev {number - 1} 0 R"
            if offset < len(bookmarks) - 1:
                links += f" /Next {number + 1} 0 R"
            pdf.write(number, b"<< /Title " + _text(mark.title) + links.encode("ascii") + b" >>")

        pdf.finish(total, catalog, info)
    partial.replace(out_pdf)
    return len(slides)
