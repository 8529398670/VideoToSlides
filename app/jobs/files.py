"""Where a job's files live. Everything is under <storage>/jobs/<id>/:

    source.<ext>      the video, until extraction succeeds (see jobs.keep_source)
    slides/           full-size slide images: the "folder of images"
    thumbs/           review-grid thumbnails, same names
    deck.pptx         the last PowerPoint export
    deck.pdf          the last PDF export
    images.zip        the slides folder, zipped for download

A combined deck (every video of a batch in one) lives in
<storage>/combined/<id>/deck.pptx and deck.pdf.

Paths come only from here, built from a job id and fixed names -- nothing a
client sends ever becomes part of a path.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from app.core.config import Config
from app.pipeline.runner import SLIDES_DIR, THUMBS_DIR

DECK_FILE = "deck.pptx"
PDF_FILE = "deck.pdf"
ZIP_FILE = "images.zip"
SOURCE_STEM = "source"


def job_dir(cfg: Config, job_id: int) -> Path:
    return cfg.jobs_dir / str(int(job_id))


def combined_dir(cfg: Config, combined_id: int) -> Path:
    return cfg.storage_dir / "combined" / str(int(combined_id))


def delete_combined_files(cfg: Config, combined_id: int) -> None:
    shutil.rmtree(combined_dir(cfg, combined_id), ignore_errors=True)


def slides_dir(cfg: Config, job_id: int) -> Path:
    return job_dir(cfg, job_id) / SLIDES_DIR


def thumbs_dir(cfg: Config, job_id: int) -> Path:
    return job_dir(cfg, job_id) / THUMBS_DIR


def source_path(cfg: Config, job_id: int, source_file: str | None) -> Path | None:
    if not source_file:
        return None
    path = job_dir(cfg, job_id) / Path(source_file).name
    return path if path.is_file() else None


def upload_part_path(cfg: Config, upload_id: str) -> Path:
    # Upload ids are url_token output; refuse anything else outright.
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", upload_id):
        raise ValueError("bad upload id")
    return cfg.uploads_dir / f"{upload_id}.part"


def safe_extension(filename: str, allowed: tuple[str, ...]) -> str | None:
    suffix = Path(filename).suffix.lower()
    return suffix if suffix in allowed else None


def download_name(title: str, extension: str) -> str:
    """A filename for Content-Disposition: the job title with anything odd
    replaced, so a title can never smuggle a path or a header break."""
    stem = re.sub(r"[^A-Za-z0-9 ._()-]+", "_", title).strip(" ._") or "slides"
    return f"{stem[:120]}{extension}"


def delete_job_files(cfg: Config, job_id: int) -> None:
    shutil.rmtree(job_dir(cfg, job_id), ignore_errors=True)


def delete_source(cfg: Config, job_id: int, source_file: str | None) -> None:
    path = source_path(cfg, job_id, source_file)
    if path is not None:
        path.unlink(missing_ok=True)


def build_zip(cfg: Config, job_id: int, slide_files: list[str], title: str) -> Path:
    """Zip the given slides (stored, not deflated: they are already JPEGs)
    into one folder named after the job."""
    target = job_dir(cfg, job_id) / ZIP_FILE
    # A uniquely named partial file, so two downloads at once cannot write
    # into the same one; the rename makes whichever finishes last the copy.
    descriptor, partial_name = tempfile.mkstemp(dir=target.parent, prefix="images.", suffix=".part")
    os.close(descriptor)
    partial = Path(partial_name)
    folder = download_name(title, "")
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_STORED) as archive:
            for name in slide_files:
                archive.write(slides_dir(cfg, job_id) / name, f"{folder}/{name}")
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target
