"""Duplicate-frame discrimination (loose -- only merges near-exact repeats)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from app.pipeline import tunables as T


# Thresholds default to None and are read from tunables at call time, so a
# change made in Settings applies without a restart.


def dhash(img: Image.Image, hash_size: int | None = None) -> np.ndarray:
    hash_size = T.HASH_SIZE if hash_size is None else hash_size
    g = img.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    arr = np.asarray(g, dtype=np.int16)
    return (arr[:, 1:] > arr[:, :-1]).flatten()


def hamming(a: np.ndarray, b: np.ndarray) -> int:
    return int(np.count_nonzero(a != b))


def region_diff_fraction(img_a: Image.Image, img_b: Image.Image, grid: int | None = None,
                         cell_thresh: float | None = None) -> float:
    grid = T.GRID if grid is None else grid
    cell_thresh = T.REGION_CELL_THRESH if cell_thresh is None else cell_thresh
    a = np.asarray(img_a.convert("L").resize((grid * 8, grid * 8)), dtype=np.float32)
    b = np.asarray(img_b.convert("L").resize((grid * 8, grid * 8)), dtype=np.float32)
    a = a.reshape(grid, 8, grid, 8).mean(axis=(1, 3))
    b = b.reshape(grid, 8, grid, 8).mean(axis=(1, 3))
    return float((np.abs(a - b) > cell_thresh).mean())


def filter_duplicates(frame_paths: list[Path], hash_dist_thresh: int | None = None,
                      min_diff: float | None = None) -> list[int]:
    """Indices of the frames to keep. A frame is kept only if BOTH the global
    perceptual hash AND the spatial region diff say it changed (AND logic) --
    see the duplicate-filtering block in tunables.py for why this matters
    with a webcam pane."""
    hash_dist_thresh = T.HASH_DIST_THRESH if hash_dist_thresh is None else hash_dist_thresh
    min_diff = T.REGION_MIN_DIFF if min_diff is None else min_diff
    keep: list[int] = []
    last_img, last_hash = None, None
    for i, fp in enumerate(frame_paths):
        with Image.open(fp) as opened:
            img = opened.copy()
        h = dhash(img)
        if last_img is None:
            keep.append(i)
            last_img, last_hash = img, h
            continue
        hd = hamming(h, last_hash)
        rdf = region_diff_fraction(last_img, img)
        if hd >= hash_dist_thresh and rdf >= min_diff:
            keep.append(i)
            last_img, last_hash = img, h
    return keep
