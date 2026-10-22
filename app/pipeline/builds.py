"""Incremental builds: find captures that the next capture merely completes.

A presenter revealing a slide piece by piece produces a chain of captures
where each one is the previous plus some content: 11 -> 12 -> 13 -> 14 in the
calibration video, of which only 14 belongs in the deck. The signal is
direction-specific: going from a build step to the next, content only
APPEARS, on an unchanged background; moving to a genuinely new slide always
removes or replaces something (at least the title, the body, a page number).

Live video on top of the slides -- a webcam pane, a mouse cursor -- changes
between any two captures and would hide that signal, so:

  - pixels the scan saw moving through most of the video (the motion map,
    see scan.py) are ignored outright, and
  - changes are tallied in cells, with a small allowance for lost content,
    which absorbs a cursor that moved.

Every threshold is in tunables.py (the "Incremental builds" block).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from app.pipeline import tunables as T

# The motion mask is grown by this much, so the edge of a webcam pane (where
# the scan's 160x90 frame is too coarse to be exact) is covered too.
MASK_GROW_PX = 6
# A pixel "changed" between the two captures when its luma moved by more than
# this. Two captures of the same static slide differ only by video
# compression noise, far below it.
CHANGE_THRESH = 60


@dataclass
class PairStats:
    build: bool
    added_cells: int
    lost_cells: int
    background_gap: float


def load_gray(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        gray = image.convert("L")
        height = max(1, round(gray.height * T.BUILD_WIDTH / gray.width))
        gray = gray.resize((T.BUILD_WIDTH, height), Image.LANCZOS).filter(ImageFilter.BoxBlur(1))
        return np.asarray(gray, dtype=np.float32)


def grow(mask: np.ndarray, radius: int) -> np.ndarray:
    """Binary dilation by a square of the given radius, with numpy shifts
    only (no scipy)."""
    if radius <= 0:
        return mask
    out = mask.copy()
    height, width = mask.shape
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dy == 0 and dx == 0:
                continue
            shifted = np.zeros_like(mask)
            ys, yd = (slice(dy, height), slice(0, height - dy)) if dy >= 0 else (slice(0, height + dy), slice(-dy, height))
            xs, xd = (slice(dx, width), slice(0, width - dx)) if dx >= 0 else (slice(0, width + dx), slice(-dx, width))
            shifted[yd, xd] = mask[ys, xs]
            out |= shifted
    return out


def motion_mask(motion: np.ndarray | None, shape: tuple[int, int]) -> np.ndarray:
    """Boolean mask, at comparison size, of pixels to ignore: those moving in
    at least MOTION_MIN_FRACTION of the scan's samples."""
    if motion is None:
        return np.zeros(shape, dtype=bool)
    moving = (np.asarray(motion, dtype=np.float32) >= T.MOTION_MIN_FRACTION).astype(np.uint8) * 255
    resized = Image.fromarray(moving).resize((shape[1], shape[0]), Image.NEAREST)
    return grow(np.asarray(resized) > 0, MASK_GROW_PX)


def background(gray: np.ndarray, valid: np.ndarray) -> float:
    """The page colour: the most common luma among the pixels compared."""
    values = gray[valid] if valid.any() else gray.ravel()
    histogram = np.bincount(np.clip(values, 0, 255).astype(np.int32) // 4, minlength=64)
    return float(histogram.argmax() * 4 + 2)


def cell_counts(mask: np.ndarray) -> int:
    """How many BUILD_CELL x BUILD_CELL cells hold at least
    BUILD_CELL_MIN_PIXELS pixels of the mask."""
    height, width = mask.shape
    rows, cols = height // T.BUILD_CELL, width // T.BUILD_CELL
    cells = mask[: rows * T.BUILD_CELL, : cols * T.BUILD_CELL].reshape(rows, T.BUILD_CELL, cols, T.BUILD_CELL).sum(axis=(1, 3))
    return int((cells >= T.BUILD_CELL_MIN_PIXELS).sum())


def compare(a: np.ndarray, b: np.ndarray, ignore: np.ndarray) -> PairStats:
    """Is A a build step of B -- does B only add content to A?"""
    if a.shape != b.shape:
        return PairStats(False, 0, 0, 255.0)
    valid = ~ignore
    bg_a, bg_b = background(a, valid), background(b, valid)
    gap = abs(bg_a - bg_b)
    if gap > T.BUILD_BG_TOLERANCE:
        return PairStats(False, 0, 0, gap)
    # The test is pixel-exact on A's content: in a real build, everything A
    # shows is still there in B, unchanged. Comparing presence of content
    # alone is not enough -- at this size a line of text is a blurry band, so
    # one sentence replaced by another in the same place would look like
    # "nothing removed".
    ink_a = (np.abs(a - bg_a) > T.BUILD_INK_THRESH) & valid
    ink_b = (np.abs(b - bg_b) > T.BUILD_INK_THRESH) & valid
    changed = (np.abs(a - b) > CHANGE_THRESH) & valid
    added = changed & ink_b & ~ink_a
    lost = changed & ink_a
    added_cells = cell_counts(added)
    lost_cells = cell_counts(lost)
    allowed_loss = max(T.BUILD_MAX_LOSS_CELLS, int(T.BUILD_MAX_LOSS_RATIO * added_cells))
    build = added_cells >= T.BUILD_MIN_ADDED_CELLS and lost_cells <= allowed_loss
    return PairStats(build, added_cells, lost_cells, gap)


def find_build_steps(frame_paths: list[Path], motion: np.ndarray | None = None) -> tuple[list[bool], list[PairStats]]:
    """For each capture, whether the next capture completes it (so it is a
    build step, superseded). The last capture is never a build step. Also
    returns the per-pair numbers, for logging and calibration. `motion` is
    the scan's map (runner.load_motion); None means nothing is ignored."""
    if len(frame_paths) < 2:
        return [False] * len(frame_paths), []
    frames = [load_gray(path) for path in frame_paths]
    # Without the scan's motion map nothing is ignored, so a live webcam pane
    # counts as changed content and blocks merging. That errs the safe way:
    # guessing the mask from the captures alone was tried, and a presenter
    # flipping between two slides made their content look "always moving".
    ignore = motion_mask(motion, frames[0].shape)
    stats = [compare(a, b, ignore) for a, b in zip(frames, frames[1:])]
    return [s.build for s in stats] + [False], stats
