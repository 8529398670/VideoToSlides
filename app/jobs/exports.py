"""Building the things a finished job turns into: a .pptx, a Google Slides
deck, a zip of the images. All synchronous -- routes call these through
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

from PIL import Image

from app.core.config import Config
from app.core.db import Store
from app.google import drive, oauth
from app.jobs import files
from app.models import google_accounts, jobs
from app.pipeline.pptx_deck import DeckSlide, build_pptx


class ExportError(Exception):
    """A failure the user can act on. The message is safe to show."""


# A deck too big for Google's import is rebuilt with lighter images before it
# is sent: full-quality frames are far more than a Slides deck needs.
COMPACT_OVER_BYTES = drive.MAX_IMPORT_BYTES - 5 * 1024 * 1024
COMPACT_MAX_WIDTH = 1600
COMPACT_QUALITY = 80


def build_deck(cfg: Config, job: jobs.Job, compact: bool = False) -> Path:
    slides = job.included_slides
    if not slides:
        raise ExportError("every slide is excluded -- include at least one")
    if not job.width or not job.height:
        raise ExportError("the job has no video size recorded")
    directory = files.slides_dir(cfg, job.id)
    deck = [
        DeckSlide(image=directory / slide["file"], start_sec=slide["start_sec"],
                  captured_sec=slide["captured_sec"], energy=slide.get("energy", 0.0))
        for slide in slides
    ]
    missing = [item.image.name for item in deck if not item.image.is_file()]
    if missing:
        raise ExportError(f"{len(missing)} slide image(s) are missing on disk")
    if not compact:
        target = files.job_dir(cfg, job.id) / files.DECK_FILE
        build_pptx(deck, job.width, job.height, target)
        return target

    # The lighter copy for Google: same slides, resized and re-encoded, in a
    # scratch folder that is removed again afterwards.
    light_dir = files.job_dir(cfg, job.id) / "compact"
    shutil.rmtree(light_dir, ignore_errors=True)
    light_dir.mkdir(parents=True)
    for item in deck:
        target_image = light_dir / item.image.name
        with Image.open(item.image) as image:
            image = image.convert("RGB")
            if image.width > COMPACT_MAX_WIDTH:
                image = image.resize((COMPACT_MAX_WIDTH, round(image.height * COMPACT_MAX_WIDTH / image.width)), Image.LANCZOS)
            image.save(target_image, "JPEG", quality=COMPACT_QUALITY, optimize=True)
        item.image = target_image
    target = files.job_dir(cfg, job.id) / "deck-google.pptx"
    try:
        build_pptx(deck, job.width, job.height, target)
    finally:
        shutil.rmtree(light_dir, ignore_errors=True)
    return target


def build_images_zip(cfg: Config, job: jobs.Job) -> Path:
    slides = job.included_slides
    if not slides:
        raise ExportError("every slide is excluded -- include at least one")
    return files.build_zip(cfg, job.id, [slide["file"] for slide in slides], job.title)


def run_export(store: Store, cfg: Config, job_id: int, kinds: list[str], gslides_new: bool = False) -> None:
    """Build what was asked for, recording each outcome on the job. Google
    needs the .pptx anyway, so it is built once and shared. The job's
    existing Google deck is replaced unless gslides_new asks for a copy."""
    job = jobs.get_job(store, job_id)
    if job is None:
        return
    deck: Path | None = None
    deck_error: str | None = None
    try:
        deck = build_deck(cfg, job)
    except ExportError as error:
        deck_error = str(error)
    except Exception as error:  # noqa: BLE001 -- recorded on the job; detail goes to the log
        traceback.print_exc(file=sys.stderr)
        deck_error = f"building the deck failed ({type(error).__name__})"

    if jobs.EXPORT_PPTX in kinds:
        jobs.finish_export(store, job_id, jobs.EXPORT_PPTX, error=deck_error)

    if jobs.EXPORT_GSLIDES in kinds:
        if deck is None:
            jobs.finish_export(store, job_id, jobs.EXPORT_GSLIDES, error=deck_error)
            return
        try:
            file_id, url = upload_to_google(store, cfg, job, deck, replace=not gslides_new)
            jobs.finish_export(store, job_id, jobs.EXPORT_GSLIDES, url=url, file_id=file_id)
        except (ExportError, oauth.GoogleError) as error:
            jobs.finish_export(store, job_id, jobs.EXPORT_GSLIDES, error=str(error))
        except Exception as error:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            jobs.finish_export(store, job_id, jobs.EXPORT_GSLIDES,
                               error=f"the Google upload failed ({type(error).__name__})")


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
    """Returns the deck's (file id, link). With replace, the job's existing
    deck is overwritten in place, keeping its link; if that deck is gone, or
    Drive will not convert into it, a new one is made and the old one
    trashed, so converting again never leaves a duplicate behind."""
    token = google_token(store, cfg, job.user_id)
    if deck.stat().st_size > COMPACT_OVER_BYTES:
        deck = build_deck(cfg, job, compact=True)
    try:
        if deck.stat().st_size > drive.MAX_IMPORT_BYTES:
            raise ExportError(
                "the deck is larger than Google's 100 MB import limit -- exclude some slides, or use the PowerPoint file"
            )
        old_id = job.gslides_file_id if replace else None
        if old_id:
            try:
                replaced = drive.replace_slides(token, old_id, deck, job.title)
            except oauth.GoogleError as failure:
                print(f"[export] job {job.id}: replacing deck in place failed ({failure}); making a new one",
                      file=sys.stderr, flush=True)
                replaced = None
            if replaced is not None:
                return replaced
        created = drive.upload_pptx_as_slides(token, deck, job.title)
        if old_id and old_id != created[0]:
            try:
                drive.trash_file(token, old_id)
            except oauth.GoogleError as failure:
                print(f"[export] job {job.id}: could not trash the old deck ({failure})", file=sys.stderr, flush=True)
        return created
    finally:
        if deck.name == "deck-google.pptx":
            deck.unlink(missing_ok=True)


def delete_google_deck(store: Store, cfg: Config, job: jobs.Job) -> None:
    """Move the job's Google Slides deck to the Drive trash."""
    file_id = job.gslides_file_id
    if file_id:
        drive.trash_file(google_token(store, cfg, job.user_id), file_id)
