"""OAuth 2.0 authorization-code flow with PKCE, for one user's Google account.

Scopes are the minimum this feature needs: drive.file (create files, and see
only the files this app created) plus openid/email so the UI can say which
account is connected. access_type=offline with prompt=consent is what makes
Google hand back a refresh token every time, so a reconnect always works.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.core.config import Config

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
SCOPES = f"openid email {DRIVE_SCOPE}"
TIMEOUT_SECONDS = 30


class GoogleError(Exception):
    """A Google call that failed. The message is safe to show."""


class GoogleScopeMissing(GoogleError):
    """Signed in, but without the Drive permission. Google's consent screen
    lists each permission with its own checkbox, so the Drive one can be left
    unticked; the result is a working sign-in that cannot create files."""

    def __init__(self) -> None:
        super().__init__(
            "Google didn't give this app access to Drive -- connect again and tick the box for "
            "\"See, edit, create and delete only the specific Google Drive files you use with this app\""
        )


def require_drive_scope(tokens: dict[str, Any]) -> None:
    granted = str(tokens.get("scope") or "").split()
    if DRIVE_SCOPE not in granted:
        raise GoogleScopeMissing()


class GoogleAuthRevoked(GoogleError):
    """The refresh token no longer works: the user removed access, or it
    expired. The stored connection should be dropped."""


def consent_url(cfg: Config, state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cfg.google_client_id,
        "redirect_uri": cfg.google_redirect_url,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


def post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=urllib.parse.urlencode(fields).encode("ascii"), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read() or b"{}")
        except ValueError:
            body = {}
        if body.get("error") == "invalid_grant":
            raise GoogleAuthRevoked("Google access was revoked or has expired") from None
        raise GoogleError(f"Google refused the request ({body.get('error') or error.code})") from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise GoogleError("could not reach Google") from None


def exchange_code(cfg: Config, code: str, verifier: str) -> tuple[str, str]:
    """(refresh_token, email) for the code Google redirected back with."""
    tokens = post_form(TOKEN_URL, {
        "code": code,
        "client_id": cfg.google_client_id,
        "client_secret": cfg.google_client_secret,
        "redirect_uri": cfg.google_redirect_url,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    })
    require_drive_scope(tokens)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise GoogleError("Google did not return a refresh token")
    return refresh_token, email_from_id_token(tokens.get("id_token", ""))


def email_from_id_token(id_token: str) -> str:
    """The id_token arrives directly from Google's token endpoint over TLS,
    so its payload can be read without verifying the signature (Google's
    documented position for exactly this case). It is used only as a label."""
    parts = id_token.split(".")
    if len(parts) != 3:
        return ""
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:
        return ""
    email = payload.get("email")
    return email if isinstance(email, str) else ""


def refresh_access_token(cfg: Config, refresh_token: str) -> str:
    tokens = post_form(TOKEN_URL, {
        "client_id": cfg.google_client_id,
        "client_secret": cfg.google_client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    # Also checked here, for connections saved before the check at connect
    # time existed (or permissions the user later removed).
    require_drive_scope(tokens)
    access_token = tokens.get("access_token")
    if not access_token:
        raise GoogleError("Google did not return an access token")
    return access_token


def revoke(refresh_token: str) -> None:
    """Best effort: the local record is deleted whatever Google says."""
    try:
        post_form(REVOKE_URL, {"token": refresh_token})
    except GoogleError:
        pass
