"""Building the things a finished job turns into: a .pptx, a PDF, a Google
Slides deck, a zip of the images -- and the same decks for a whole batch at once
(app/models/combined.py). All synchronous -- routes call these through
run(), and the export endpoint runs them as a background task so a slow
Drive upload never holds a request open.

Decks are built from the slides the user has not excluded, in order, and
rebuilt on every export, so excluding a slide and exporting again does what
it says.
"""
from __future__ import annotations

import shutil
import sys
import traceback
from pathlib import Path
from typing import Callable

from PIL import Image

from app.core.config import Config
from app.core.db import Store
from app.google import drive, oauth
from app.jobs import files
from app.models import combined, google_accounts, jobs
from app.pipeline.pdf_deck import Bookmark, build_pdf
from app.pipeline.pptx_deck import DeckSlide, build_pptx


class ExportError(Exception):
    """A failure the user can act on. The message is safe to show."""


# A deck too big for Google's import is rebuilt with lighter images before it
# is sent: full-quality frames are far more than a Slides deck needs.
COMPACT_OVER_BYTES = drive.MAX_IMPORT_BYTES - 5 * 1024 * 1024
COMPACT_MAX_WIDTH = 1600
COMPACT_QUALITY = 80
GOOGLE_DECK_FILE = "deck-google.pptx"


def _job_slides(cfg: Config, job: jobs.Job, label: str | None = None) -> list[DeckSlide]:
    directory = files.slides_dir(cfg, job.id)
    size = (job.width, job.height) if job.width and job.height else None
    return [
        DeckSlide(image=directory / slide["file"], start_sec=slide["start_sec"],
                  captured_sec=slide["captured_sec"], energy=slide.get("energy", 0.0), label=label, size=size)
        for slide in job.included_slides
    ]


def playlist_lines(job: jobs.Job) -> list[str]:
    playlist = job.playlist or {}
    if not playlist.get("url"):
        return []
    return [f"Playlist: {playlist.get('title') or ''}".rstrip(), playlist["url"]]


def video_lines(job: jobs.Job, heading: str) -> list[str]:
    """The video's title and links: its own page, and the same video opened
    inside its playlist, at its place in it."""
    lines = [f"{heading}: {job.title}"]
    if job.source_url:
        lines.append(job.source_url)
    playlist = job.playlist or {}
    if playlist.get("video_url"):
        where = f" (video #{playlist['index']})" if playlist.get("index") else ""
        lines.append(f"In the playlist{where}: {playlist['video_url']}")
    return lines


def _check_images(deck: list[DeckSlide]) -> None:
    missing = [item.image.name for item in deck if not item.image.is_file()]
    if missing:
        raise ExportError(f"{len(missing)} slide image(s) are missing on disk")


def _write_deck(deck: list[DeckSlide], width: int, height: int, directory: Path, compact: bool) -> Path:
    _check_images(deck)
    directory.mkdir(parents=True, exist_ok=True)
    if not compact:
        target = directory / files.DECK_FILE
        build_pptx(deck, width, height, target)
        return target

    # The lighter copy for Google: same slides, resized and re-encoded, in a
    # scratch folder that is removed again afterwards. Numbered, because a
    # combined deck has a "1.jpg" from every video.
    light_dir = directory / "compact"
    shutil.rmtree(light_dir, ignore_errors=True)
    light_dir.mkdir(parents=True)
    for index, item in enumerate(deck):
        target_image = light_dir / f"{index:05d}.jpg"
        with Image.open(item.image) as image:
            image = image.convert("RGB")
            if image.width > COMPACT_MAX_WIDTH:
                image = image.resize((COMPACT_MAX_WIDTH, round(image.height * COMPACT_MAX_WIDTH / image.width)), Image.LANCZOS)
            image.save(target_image, "JPEG", quality=COMPACT_QUALITY, optimize=True)
        item.image = target_image
    target = directory / GOOGLE_DECK_FILE
    try:
        build_pptx(deck, width, height, target)
    finally:
        shutil.rmtree(light_dir, ignore_errors=True)
    return target


def _single_deck(cfg: Config, job: jobs.Job) -> list[DeckSlide]:
    if not job.included_slides:
        raise ExportError("every slide is excluded -- include at least one")
    if not job.width or not job.height:
        raise ExportError("the job has no video size recorded")
    deck = _job_slides(cfg, job)
    # The first slide's notes say where the video came from.
    intro = playlist_lines(job)
    deck[0].intro = "\n".join(intro + ([""] if intro else []) + video_lines(job, "Video"))
    return deck


def build_deck(cfg: Config, job: jobs.Job, compact: bool = False) -> Path:
    return _write_deck(_single_deck(cfg, job), job.width, job.height, files.job_dir(cfg, job.id), compact)


def build_job_pdf(cfg: Config, job: jobs.Job) -> Path:
    deck = _single_deck(cfg, job)
    _check_images(deck)
    target = files.job_dir(cfg, job.id) / files.PDF_FILE
    build_pdf(deck, job.width, job.height, target, job.title)
    return target


def combined_jobs(store: Store, combo: combined.Combined) -> list[jobs.Job]:
    """The videos going into a combined deck, in order: still there, still
    the owner's, finished, with at least one slide included."""
    found = []
    for job_id in combo.job_ids:
        if job_id in combo.left_out:
            continue
        job = jobs.get_job(store, job_id)
        if (job is not None and job.user_id == combo.user_id and job.status == jobs.STATUS_READY
                and job.included_slides and job.width and job.height):
            found.append(job)
    return found


def _combined_deck(store: Store, cfg: Config, combo: combined.Combined) -> tuple[list[DeckSlide], jobs.Job, list[Bookmark]]:
    """Every chosen video's included slides, video after video, the first
    video (returned too) setting the slide size; and a bookmark at the start
    of each video, for the PDF."""
    videos = combined_jobs(store, combo)
    if not videos:
        raise ExportError("none of the chosen videos has slides to include")
    # The first slide's notes name the playlist; every video's first slide
    # names that video, with its links.
    playlist = next((lines for lines in map(playlist_lines, videos) if lines), [])
    deck: list[DeckSlide] = []
    bookmarks: list[Bookmark] = []
    for number, job in enumerate(videos, 1):
        slides = _job_slides(cfg, job, label=job.title)
        intro = video_lines(job, f"Video {number} of {len(videos)}")
        if number == 1 and playlist:
            intro = playlist + [""] + intro
        slides[0].intro = "\n".join(intro)
        bookmarks.append(Bookmark(title=job.title, page=len(deck)))
        deck.extend(slides)
    return deck, videos[0], bookmarks


def build_combined_deck(store: Store, cfg: Config, combo: combined.Combined, compact: bool = False) -> Path:
    deck, first, _ = _combined_deck(store, cfg, combo)
    return _write_deck(deck, first.width, first.height, files.combined_dir(cfg, combo.id), compact)


def build_combined_pdf(store: Store, cfg: Config, combo: combined.Combined) -> Path:
    deck, first, bookmarks = _combined_deck(store, cfg, combo)
    _check_images(deck)
    directory = files.combined_dir(cfg, combo.id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / files.PDF_FILE
    build_pdf(deck, first.width, first.height, target, combo.title, bookmarks)
    return target


def build_images_zip(cfg: Config, job: jobs.Job) -> Path:
    slides = job.included_slides
    if not slides:
        raise ExportError("every slide is excluded -- include at least one")
    return files.build_zip(cfg, job.id, [slide["file"] for slide in slides], job.title)


def _attempt(build: Callable[[], Path]) -> tuple[Path | None, str | None]:
    """(the built file, None) or (None, why it failed)."""
    try:
        return build(), None
    except ExportError as error:
        return None, str(error)
    except Exception as error:  # noqa: BLE001 -- recorded on the record; detail goes to the log
        traceback.print_exc(file=sys.stderr)
        return None, f"building the file failed ({type(error).__name__})"


def _run(kinds: list[str], build: Callable[[], Path], build_pdf_file: Callable[[], Path],
         finish: Callable[..., None], upload: Callable[[Path], tuple[str, str]]) -> None:
    """Build what was asked for, recording each outcome through
    finish(kind, error=, url=, file_id=). The .pptx is built once and shared
    with Google, which imports it; the PDF is built on its own."""
    if jobs.EXPORT_PDF in kinds:
        _, pdf_error = _attempt(build_pdf_file)
        finish(jobs.EXPORT_PDF, error=pdf_error)
    if jobs.EXPORT_PPTX not in kinds and jobs.EXPORT_GSLIDES not in kinds:
        return
    deck, deck_error = _attempt(build)

    if jobs.EXPORT_PPTX in kinds:
        finish(jobs.EXPORT_PPTX, error=deck_error)

    if jobs.EXPORT_GSLIDES in kinds:
        if deck is None:
            finish(jobs.EXPORT_GSLIDES, error=deck_error)
            return
        try:
            file_id, url = upload(deck)
            finish(jobs.EXPORT_GSLIDES, url=url, file_id=file_id)
        except (ExportError, oauth.GoogleError) as error:
            finish(jobs.EXPORT_GSLIDES, error=str(error))
        except Exception as error:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            finish(jobs.EXPORT_GSLIDES, error=f"the Google upload failed ({type(error).__name__})")


def run_export(store: Store, cfg: Config, job_id: int, kinds: list[str], gslides_new: bool = False) -> None:
    """Build what was asked for, recording each outcome on the job. The
    job's existing Google deck is replaced unless gslides_new asks for a
    copy."""
    job = jobs.get_job(store, job_id)
    if job is None:
        return
    _run(
        kinds,
        lambda: build_deck(cfg, job),
        lambda: build_job_pdf(cfg, job),
        lambda kind, **outcome: jobs.finish_export(store, job_id, kind, **outcome),
        lambda deck: upload_to_google(store, cfg, job, deck, replace=not gslides_new),
    )


def run_combined_export(store: Store, cfg: Config, combined_id: int, kinds: list[str], gslides_new: bool = False) -> None:
    """run_export for a whole batch in one deck."""
    combo = combined.get(store, combined_id)
    if combo is None:
        return
    _run(
        kinds,
        lambda: build_combined_deck(store, cfg, combo),
        lambda: build_combined_pdf(store, cfg, combo),
        lambda kind, **outcome: combined.finish_export(store, combined_id, kind, **outcome),
        lambda deck: send_to_google(
            store, cfg, combo.user_id, combo.title, combo.gslides_file_id if not gslides_new else None, deck,
            lambda: build_combined_deck(store, cfg, combo, compact=True), f"combined deck {combo.id}",
        ),
    )


def google_token(store: Store, cfg: Config, user_id: int) -> str:
    """A fresh access token for the user's connected Google account."""
    if not cfg.google_enabled:
        raise ExportError("Google Slides isn't set up on this server yet (Settings -> Google Slides)")
    account = google_accounts.get_account(store, user_id)
    if account is None:
        raise ExportError("connect your Google account first (Profile -> Google Slides)")
    try:
        return oauth.refresh_access_token(cfg, account.refresh_token)
    except oauth.GoogleAuthRevoked:
        google_accounts.delete_account(store, user_id)
        raise ExportError("Google access was revoked -- connect your Google account again") from None
    except oauth.GoogleScopeMissing as failure:
        # A connection that can never upload; drop it so the next Convert
        # offers to connect again instead of failing the same way.
        google_accounts.delete_account(store, user_id)
        raise ExportError(str(failure)) from None


def upload_to_google(store: Store, cfg: Config, job: jobs.Job, deck: Path, replace: bool = True) -> tuple[str, str]:
    return send_to_google(
        store, cfg, job.user_id, job.title, job.gslides_file_id if replace else None, deck,
        lambda: build_deck(cfg, job, compact=True), f"job {job.id}",
    )


def send_to_google(store: Store, cfg: Config, user_id: int, title: str, old_id: str | None, deck: Path,
                   build_compact: Callable[[], Path], what: str) -> tuple[str, str]:
    """Returns the deck's (file id, link). With an old_id, that deck is
    overwritten in place, keeping its link; if it is gone, or Drive will not
    convert into it, a new one is made and the old one trashed, so
    converting again never leaves a duplicate behind."""
    token = google_token(store, cfg, user_id)
    if deck.stat().st_size > COMPACT_OVER_BYTES:
        deck = build_compact()
    try:
        if deck.stat().st_size > drive.MAX_IMPORT_BYTES:
            raise ExportError(
                "the deck is larger than Google's 100 MB import limit -- exclude some slides, or use the PowerPoint file"
            )
        if old_id:
            try:
                replaced = drive.replace_slides(token, old_id, deck, title)
            except oauth.GoogleError as failure:
                print(f"[export] {what}: replacing deck in place failed ({failure}); making a new one",
                      file=sys.stderr, flush=True)
                replaced = None
            if replaced is not None:
                return replaced
        created = drive.upload_pptx_as_slides(token, deck, title)
        if old_id and old_id != created[0]:
            try:
                drive.trash_file(token, old_id)
            except oauth.GoogleError as failure:
                print(f"[export] {what}: could not trash the old deck ({failure})", file=sys.stderr, flush=True)
        return created
    finally:
        if deck.name == GOOGLE_DECK_FILE:
            deck.unlink(missing_ok=True)


def delete_google_deck(store: Store, cfg: Config, job: jobs.Job | combined.Combined) -> None:
    """Move a job's or a combined deck's Google Slides deck to the Drive trash."""
    file_id = job.gslides_file_id
    if file_id:
        drive.trash_file(google_token(store, cfg, job.user_id), file_id)
