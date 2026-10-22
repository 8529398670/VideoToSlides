"""Upload a .pptx to the user's Drive as a Google Slides deck.

Drive converts on import when the target mimeType is a Google format, which
is far simpler than building a deck through the Slides API (that needs every
image at a public URL). A resumable upload is used because decks of
full-resolution slides run to tens of megabytes.

Converting again uploads into the same file (Drive converts on update too),
so the deck keeps its link and Drive keeps the earlier version in its
history. The drive.file scope covers updating and trashing files this app
created, and nothing else.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from app.google.oauth import TIMEOUT_SECONDS, GoogleError

UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id,webViewLink"
UPDATE_URL = "https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=resumable&fields=id,webViewLink"
FILE_URL = "https://www.googleapis.com/drive/v3/files/{file_id}"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
SLIDES_MIME = "application/vnd.google-apps.presentation"
# Google converts presentations up to 100 MB.
MAX_IMPORT_BYTES = 100 * 1024 * 1024
UPLOAD_TIMEOUT_SECONDS = 600


def _error(error: urllib.error.HTTPError, action: str = "the upload") -> GoogleError:
    try:
        message = json.loads(error.read() or b"{}").get("error", {}).get("message", "")
    except (ValueError, AttributeError):
        message = ""
    return GoogleError(f"Google Drive refused {action} ({message or error.code})")


def _json_call(access_token: str, url: str, method: str, body: dict | None, action: str) -> dict | None:
    """A small metadata call. Returns None on 404 (the file is gone, or was
    never this app's to see)."""
    headers = {"Authorization": f"Bearer {access_token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=UTF-8"
    request = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise _error(error, action) from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise GoogleError("could not reach Google Drive") from None


def _resumable_upload(access_token: str, start_url: str, method: str, metadata: dict, pptx: Path) -> tuple[str, str]:
    """Start a resumable session, send the whole file, and return the
    file's (id, webViewLink)."""
    size = pptx.stat().st_size
    start = urllib.request.Request(
        start_url, method=method,
        data=json.dumps(metadata).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": PPTX_MIME,
            "X-Upload-Content-Length": str(size),
        },
    )
    try:
        with urllib.request.urlopen(start, timeout=TIMEOUT_SECONDS) as response:
            session_url = response.headers.get("Location")
    except urllib.error.HTTPError as error:
        raise _error(error) from None
    except (urllib.error.URLError, TimeoutError):
        raise GoogleError("could not reach Google Drive") from None
    if not session_url:
        raise GoogleError("Google Drive did not start the upload")

    with pptx.open("rb") as handle:
        put = urllib.request.Request(
            session_url, method="PUT", data=handle,
            headers={"Content-Type": PPTX_MIME, "Content-Length": str(size)},
        )
        try:
            with urllib.request.urlopen(put, timeout=UPLOAD_TIMEOUT_SECONDS) as response:
                created = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            raise _error(error) from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise GoogleError("the upload to Google Drive did not complete") from None

    file_id = created.get("id")
    if not file_id:
        raise GoogleError("Google Drive did not return the deck")
    link = created.get("webViewLink") or f"https://docs.google.com/presentation/d/{file_id}/edit"
    return file_id, link


def upload_pptx_as_slides(access_token: str, pptx: Path, name: str) -> tuple[str, str]:
    """A new deck. Returns its (file id, webViewLink)."""
    return _resumable_upload(access_token, UPLOAD_URL, "POST", {"name": name, "mimeType": SLIDES_MIME}, pptx)


def replace_slides(access_token: str, file_id: str, pptx: Path, name: str) -> tuple[str, str] | None:
    """Upload into an existing deck, replacing its slides. Returns None when
    there is nothing to replace -- the deck was deleted or is in the trash --
    so the caller makes a new one."""
    found = _json_call(access_token, FILE_URL.format(file_id=file_id) + "?fields=id,trashed", "GET", None,
                       "access to the deck")
    if found is None or found.get("trashed"):
        return None
    return _resumable_upload(access_token, UPDATE_URL.format(file_id=file_id), "PATCH", {"name": name}, pptx)


def trash_file(access_token: str, file_id: str) -> None:
    """Move a deck to the Drive trash, where it can still be restored for
    30 days. A deck that is already gone counts as done."""
    _json_call(access_token, FILE_URL.format(file_id=file_id) + "?fields=id", "PATCH", {"trashed": True},
               "moving the deck to the trash")
