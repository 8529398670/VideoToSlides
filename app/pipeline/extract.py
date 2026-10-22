"""Grab the frame at each capture time, and make review thumbnails."""
from __future__ import annotations

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from PIL import Image

from app.pipeline import tunables as T

GRAB_TIMEOUT_SECONDS = 60


def extract_frames_bulk(video_path: str, timestamps: list[float], out_dir: Path, max_workers: int,
                        on_progress: Callable[[int, int], None] | None = None) -> list[Path | None]:
    """Extract each capture frame with its own ffmpeg process, parallelized
    with a thread pool. (A single-pass 'select'-filter extraction was tried
    to amortize ffmpeg startup cost but silently produced wrong frame counts
    on real recordings; per-frame extraction is the reliable path.)

    Returns one entry per timestamp: the frame's path, or None where that
    grab failed -- a rare per-frame failure just drops that candidate."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_paths = [out_dir / f"frame_{i:05d}.jpg" for i in range(len(timestamps))]
    lock = threading.Lock()
    done = [0]

    def grab(args: tuple[float, Path]) -> None:
        ts, out_path = args
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-nostdin", "-v", "error", "-ss", f"{ts:.3f}", "-i", video_path,
                 "-frames:v", "1", "-q:v", str(T.JPEG_QUALITY), "-threads", "1", str(out_path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=True, timeout=GRAB_TIMEOUT_SECONDS,
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            pass
        with lock:
            done[0] += 1
            count = done[0]
        if on_progress is not None:
            on_progress(count, len(timestamps))

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        list(pool.map(grab, zip(timestamps, out_paths)))

    return [p if p.is_file() and p.stat().st_size > 0 else None for p in out_paths]


def make_thumbnail(source: Path, target: Path) -> None:
    with Image.open(source) as image:
        image = image.convert("RGB")
        ratio = T.THUMB_WIDTH / image.width
        if ratio < 1:
            image = image.resize((T.THUMB_WIDTH, max(1, round(image.height * ratio))), Image.LANCZOS)
        image.save(target, "JPEG", quality=80, optimize=True)
