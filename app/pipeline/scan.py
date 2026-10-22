"""Coarse scan: find the moments the picture changes enough to be a new slide.

The video is split into time chunks and each chunk is decoded by its own
ffmpeg process, which also does the downscale. Python only ever sees tiny
SCAN_WIDTH x SCAN_HEIGHT frames, so the per-frame work here is one numpy
subtraction -- the cores are spent decoding, inside ffmpeg, where they belong.

This replaces the original OpenCV reader. ffmpeg hands back RGB (full range,
area-averaged) and the luma below uses the same BT.601 weights as
cv2.cvtColor(..., COLOR_BGR2GRAY), so COARSE_DIFF_THRESH means what it
meant when it was calibrated.
"""
from __future__ import annotations

import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import numpy as np

from app.pipeline.probe import VideoInfo
from app.pipeline import tunables as T

Candidate = dict  # {"start_sec": float, "energy": float}
Progress = Callable[[int, int], None]  # (frames scanned so far, total frames)

FRAME_BYTES = T.SCAN_WIDTH * T.SCAN_HEIGHT * 3
LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)
CELL = 10  # gradual-change cells are CELL x CELL pixels of the scan frame


def changed_area(a: np.ndarray, b: np.ndarray) -> float:
    """Fraction of CELL x CELL cells whose mean luminance moved by more than
    GRADUAL_CELL_THRESH: how much of the picture changed, rather than by how
    much on average."""
    rows, cols = T.SCAN_HEIGHT // CELL, T.SCAN_WIDTH // CELL
    delta = np.abs(a - b)[: rows * CELL, : cols * CELL].reshape(rows, CELL, cols, CELL).mean(axis=(1, 3))
    return float((delta > T.GRADUAL_CELL_THRESH).mean())


def _gray(raw: bytes) -> np.ndarray:
    rgb = np.frombuffer(raw, dtype=np.uint8).reshape(T.SCAN_HEIGHT, T.SCAN_WIDTH, 3)
    return np.rint(rgb.astype(np.float32) @ LUMA).astype(np.int16)


def _sampler(video_path: str, start_sec: float, duration_sec: float) -> list[str]:
    """ffmpeg emitting every STRIDE-th frame of [start, start+duration) as raw
    SCAN_WIDTH x SCAN_HEIGHT RGB. Frame n of the output is frame
    start + n*STRIDE of the input -- the same frames the original grab()/read()
    loop compared."""
    filters = (
        f"select=not(mod(n\\,{T.STRIDE})),"
        f"scale={T.SCAN_WIDTH}:{T.SCAN_HEIGHT}:flags=area:out_range=full,format=rgb24"
    )
    return [
        "ffmpeg", "-nostdin", "-v", "error",
        "-ss", f"{start_sec:.3f}", "-i", video_path, "-t", f"{duration_sec:.3f}",
        "-an", "-sn", "-dn", "-threads", "1",
        "-vf", filters, "-fps_mode", "passthrough",
        "-f", "rawvideo", "-",
    ]


@dataclass
class ScanResult:
    candidates: list[Candidate]
    # Per scan-frame pixel: in what fraction of samples it moved. Live video
    # layered over the slides (a webcam pane) moves in most samples; slide
    # content almost never. builds.py ignores the former.
    motion: np.ndarray


def scan_range(video_path: str, start_frame: int, end_frame: int, fps: float,
               on_frames: Callable[[int], None] | None = None) -> tuple[list[Candidate], np.ndarray, int]:
    """Sequential single pass over one chunk: one seek to its start, then
    forward decoding only.

    Two tests per sample: against the previous sample (a hard cut), and
    against the sample GRADUAL_WINDOW_SEC back (a fade too gradual for the
    first). See GRADUAL_WINDOW_SEC for why the second is held off after a
    cut."""
    command = _sampler(video_path, start_frame / fps, (end_frame - start_frame) / fps)
    candidates: list[Candidate] = []
    moved = np.zeros((T.SCAN_HEIGHT, T.SCAN_WIDTH), dtype=np.int32)
    samples = 0
    window = max(2, round(T.GRADUAL_WINDOW_SEC * fps / T.STRIDE))
    history: deque[np.ndarray] = deque(maxlen=window)
    last_cut = float("-inf")

    def fire(cut_time: float, energy: float) -> None:
        nonlocal last_cut
        last_cut = cut_time
        if not candidates or (cut_time - candidates[-1]["start_sec"]) >= T.MIN_GAP_SEC:
            candidates.append({"start_sec": cut_time, "energy": round(energy, 2)})

    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert process.stdout is not None
        first = process.stdout.read(FRAME_BYTES)
        if len(first) < FRAME_BYTES:
            return candidates, moved, samples
        prev_gray = _gray(first)
        cur_idx = start_frame
        while True:
            raw = process.stdout.read(FRAME_BYTES)
            if len(raw) < FRAME_BYTES:
                break
            cur_idx += T.STRIDE
            if cur_idx >= end_frame:
                break
            gray = _gray(raw)
            history.append(prev_gray)
            # The in-between frames were never compared, so the sampled frame
            # is the cut point -- accurate to within STRIDE frames, well inside
            # the capture window used downstream anyway.
            cut_time = cur_idx / fps
            delta = np.abs(gray - prev_gray)
            moved += delta > T.MOTION_PIXEL_THRESH
            samples += 1
            diff = float(delta.mean())
            if diff >= T.COARSE_DIFF_THRESH:
                fire(cut_time, diff)
            elif len(history) == window and cut_time - last_cut >= T.GRADUAL_WINDOW_SEC:
                gradual = float(np.abs(gray - history[0]).mean())
                if gradual >= T.COARSE_DIFF_THRESH and changed_area(gray, history[0]) >= T.GRADUAL_MIN_AREA:
                    fire(cut_time, gradual)
            prev_gray = gray
            if on_frames is not None:
                on_frames(T.STRIDE)
    finally:
        if process.stdout is not None:
            process.stdout.close()
        process.kill()
        process.wait()
    return candidates, moved, samples


def detect_candidates_parallel(video_path: str, info: VideoInfo, n_workers: int,
                               on_progress: Progress | None = None) -> ScanResult:
    """Scan the video in n_workers parallel chunks and merge the results into
    one time-ordered candidate list."""
    nb_frames = info.nb_frames
    n_workers = max(1, n_workers)
    chunk_frames = max(1, nb_frames // n_workers)
    bounds = []
    for i in range(n_workers):
        start_f = i * chunk_frames
        end_f = nb_frames if i == n_workers - 1 else (i + 1) * chunk_frames
        if start_f < end_f:
            bounds.append((start_f, end_f))

    lock = threading.Lock()
    done = [0]

    def advance(frames: int) -> None:
        with lock:
            done[0] += frames
            scanned = min(done[0], nb_frames)
        if on_progress is not None:
            on_progress(scanned, nb_frames)

    with ThreadPoolExecutor(max_workers=len(bounds)) as pool:
        results = list(pool.map(lambda b: scan_range(video_path, b[0], b[1], info.fps, advance), bounds))

    all_candidates = sorted((c for chunk in results for c in chunk[0]), key=lambda c: c["start_sec"])
    total_samples = sum(chunk[2] for chunk in results)
    moved = sum((chunk[1] for chunk in results), np.zeros((T.SCAN_HEIGHT, T.SCAN_WIDTH), dtype=np.int32))
    motion = (moved / max(1, total_samples)).astype(np.float32)
    # Merge candidates that fall within MIN_GAP_SEC across chunk boundaries.
    merged: list[Candidate] = []
    for c in all_candidates:
        if merged and (c["start_sec"] - merged[-1]["start_sec"]) < T.MIN_GAP_SEC:
            continue
        merged.append(c)
    return ScanResult(candidates=merged, motion=motion)
