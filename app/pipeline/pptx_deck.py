"""Assemble a .pptx: one full-bleed picture per slide, timing in the notes."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pptx import Presentation
from pptx.util import Emu

from app.pipeline import tunables as T

EMU_PER_IN = 914400
BLANK_LAYOUT = 6


@dataclass
class DeckSlide:
    image: Path
    start_sec: float
    captured_sec: float
    energy: float


def build_pptx(slides: list[DeckSlide], width_px: int, height_px: int, out_pptx: Path) -> int:
    aspect = width_px / height_px
    slide_w_in = T.SLIDE_WIDTH_IN
    slide_h_in = slide_w_in / aspect
    prs = Presentation()
    prs.slide_width = Emu(int(slide_w_in * EMU_PER_IN))
    prs.slide_height = Emu(int(slide_h_in * EMU_PER_IN))
    blank = prs.slide_layouts[BLANK_LAYOUT]

    for item in slides:
        slide = prs.slides.add_slide(blank)
        slide.shapes.add_picture(str(item.image), 0, 0, width=prs.slide_width, height=prs.slide_height)
        notes = slide.notes_slide.notes_text_frame
        notes.text = (f"Scene Start: {item.start_sec:.2f}s\n"
                      f"Captured at: {item.captured_sec:.2f}s\n"
                      f"Energy: {item.energy:.2f}")

    # Written beside the target and renamed, so a download never sees half a file.
    partial = out_pptx.with_suffix(".pptx.part")
    prs.save(str(partial))
    partial.replace(out_pptx)
    return len(slides)
