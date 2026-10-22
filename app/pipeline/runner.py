"""One video in, one folder of slide images out -- the stages in order.

Progress goes through `report(stage, fraction)`; stage is one of the STAGE_*
names below, and the job model uses the same names as job statuses.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from app.pipeline import builds, dedupe, detect, extract, probe, scan
from app.pipeline import tunables as T

STAGE_SCANNING = "scanning"
STAGE_EXTRACTING = "extracting"
STAGE_FILTERING = "filtering"

SLIDES_DIR = "slides"
THUMBS_DIR = "thumbs"
WORK_DIR = "work"
# The scan's motion map, kept so build detection can be re-run on a finished
# job without its video (see `manage detect-builds`).
MOTION_FILE = "motion.png"

Report = Callable[[str, float], None]


@dataclass
class Slide:
    n: int
    file: str
    start_sec: float
    captured_sec: float
    energy: float
    # Superseded by the next slide, which only adds content to it (an
    # incremental build). Kept on disk and in the review grid, but excluded
    # from decks by default.
    build_step: bool = False


@dataclass
class Result:
    width: int
    height: int
    fps: float
    duration_sec: float
    slides: list[Slide] = field(default_factory=list)
    # How each stage narrowed things down -- shown in the UI and the log.
    counts: dict[str, int] = field(default_factory=dict)


def slide_name(n: int) -> str:
    return f"slide_{n:04d}.jpg"


def reset_output(out_dir: Path) -> None:
    for name in (SLIDES_DIR, THUMBS_DIR, WORK_DIR):
        shutil.rmtree(out_dir / name, ignore_errors=True)
    (out_dir / MOTION_FILE).unlink(missing_ok=True)


def save_motion(motion: np.ndarray, out_dir: Path) -> None:
    Image.fromarray(np.clip(motion * 255, 0, 255).astype(np.uint8)).save(out_dir / MOTION_FILE)


def load_motion(out_dir: Path) -> np.ndarray | None:
    path = out_dir / MOTION_FILE
    if not path.is_file():
        return None
    with Image.open(path) as image:
        return np.asarray(image.convert("L"), dtype=np.float32) / 255.0


def mark_build_steps(slides: list[Slide], slides_dir: Path, motion: np.ndarray | None,
                     log: Callable[[str], None] = print) -> int:
    """Flag the slides that the next slide completes, and date each completed
    slide from when its first build step appeared. Returns how many were
    flagged."""
    if not T.BUILD_MERGE_ENABLED or len(slides) < 2:
        return 0
    flags, _ = builds.find_build_steps([slides_dir / slide.file for slide in slides], motion)
    chain_start: float | None = None
    for slide, flag in zip(slides, flags):
        slide.build_step = flag
        if flag:
            chain_start = slide.start_sec if chain_start is None else chain_start
        elif chain_start is not None:
            slide.start_sec = chain_start
            chain_start = None
    count = sum(flags)
    if count:
        log(f"{count} slide(s) are build steps of the slide after them (excluded by default)")
    return count


def process_video(video_path: Path, out_dir: Path, scan_workers: int, extract_workers: int,
                  report: Report, log: Callable[[str], None] = print) -> Result:
    reset_output(out_dir)
    info = probe.get_video_info(str(video_path))
    duration_sec = info.duration_sec

    report(STAGE_SCANNING, 0.0)
    scanned = scan.detect_candidates_parallel(
        str(video_path), info, scan_workers, lambda done, total: report(STAGE_SCANNING, done / total)
    )
    all_cuts = scanned.candidates
    save_motion(scanned.motion, out_dir)
    log(f"{len(all_cuts)} candidate slide-cuts over {duration_sec:.0f}s of video")

    scenes = detect.suppress_noise_runs(all_cuts)
    if len(scenes) < len(all_cuts):
        log(f"suppressed {len(all_cuts) - len(scenes)} candidate(s) in dense noise run(s) "
            "(likely talking-head/dead-air, no slide on screen)")
    after_noise = len(scenes)
    scenes = detect.add_opening_slide(scenes, all_cuts)
    scenes = detect.merge_cut_bursts(scenes)
    log(f"{len(scenes)} slide(s) after transition-burst merging")

    report(STAGE_EXTRACTING, 0.0)
    timestamps = detect.compute_capture_times(scenes, all_cuts, duration_sec)
    work_dir = out_dir / WORK_DIR
    grabbed = extract.extract_frames_bulk(
        str(video_path), timestamps, work_dir, extract_workers,
        lambda done, total: report(STAGE_EXTRACTING, done / total),
    )
    present = [i for i, path in enumerate(grabbed) if path is not None]
    log(f"extracted {len(present)}/{len(scenes)} candidate frames")

    report(STAGE_FILTERING, 0.0)
    frame_paths = [grabbed[i] for i in present]
    kept = [present[k] for k in dedupe.filter_duplicates(frame_paths)]
    log(f"{len(kept)}/{len(present)} kept after duplicate filtering")

    slides_dir = out_dir / SLIDES_DIR
    thumbs_dir = out_dir / THUMBS_DIR
    slides_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    result = Result(width=info.width, height=info.height, fps=info.fps, duration_sec=duration_sec)
    for n, index in enumerate(kept, start=1):
        name = slide_name(n)
        source = grabbed[index]
        assert source is not None
        source.replace(slides_dir / name)
        extract.make_thumbnail(slides_dir / name, thumbs_dir / name)
        result.slides.append(Slide(
            n=n, file=name,
            start_sec=round(scenes[index]["start_sec"], 3),
            captured_sec=round(timestamps[index], 3),
            energy=scenes[index]["energy"],
        ))
        report(STAGE_FILTERING, n / max(1, len(kept)))
    shutil.rmtree(work_dir, ignore_errors=True)
    build_steps = mark_build_steps(result.slides, slides_dir, scanned.motion, log)

    result.counts = {
        "candidates": len(all_cuts),
        "after_noise": after_noise,
        "after_bursts": len(scenes),
        "extracted": len(present),
        "slides": len(kept),
        "build_steps": build_steps,
    }
    return result
