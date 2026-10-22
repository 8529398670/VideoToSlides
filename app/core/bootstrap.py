"""Solves the chicken-and-egg problem this auth model creates: only an admin
can mint a login link, and on a fresh database there is no admin to do it.

So on first boot -- and only when no enabled admin exists -- the server
creates one and prints a one-time login link to its own logs. Whoever can read
the container logs is already trusted with the deployment, so this grants
nothing they did not already have, and it means a fresh deploy is usable
without a separate setup command.

Every path that reveals a credential prints through this module, so they all
have one shape and one warning.
"""
from __future__ import annotations

from app.core.config import Config
from app.core.db import Store
from app.models import login_tokens, users

RULE = "-" * 72


def describe_seconds(seconds: int) -> str:
    if seconds % 86400 == 0:
        count, unit = seconds // 86400, "day"
    elif seconds % 3600 == 0:
        count, unit = seconds // 3600, "hour"
    elif seconds % 60 == 0:
        count, unit = seconds // 60, "minute"
    else:
        count, unit = seconds, "second"
    return f"{count} {unit}{'' if count == 1 else 's'}"


def ensure_first_admin(store: Store, cfg: Config, display_name: str = "Admin") -> None:
    """A no-op once any enabled admin exists, so restarting the container does
    not keep minting new admin accounts."""
    if users.any_admin_exists(store):
        return
    user = users.create_user(store, display_name, users.ROLE_ADMIN)
    credential = login_tokens.issue_login_token(store, user.id, cfg.login_token_ttl_seconds)
    print_login_link(cfg, f"first-run admin {display_name!r}", credential)


def print_login_link(cfg: Config, label: str, credential: str) -> None:
    """The one moment a login credential is ever visible. It is shown as a path
    because the server genuinely does not know its public origin -- behind a
    reverse proxy it sees an internal address, and printing a confidently
    wrong URL is worse than printing an obviously partial one."""
    lines = [
        RULE,
        f"  ONE-TIME LOGIN LINK -- {label}",
        RULE,
        f"  /login/{credential}",
        RULE,
        f"  Valid once, for {describe_seconds(cfg.login_token_ttl_seconds)}. Prepend your real scheme and host, e.g.",
        f"    https://your-host.example.com/login/{credential}",
        "  It is not stored anywhere and cannot be shown again -- if you lose",
        "  it, mint another with: python -m app manage reissue-login --user-id <id>",
        RULE,
    ]
    print("\n".join(lines), flush=True)


def print_api_key(label: str, credential: str, expires: str) -> None:
    """The one moment an API key is ever visible. Unlike a login link this is
    a complete credential, not a URL, so there is no host to guess at -- it
    goes in an Authorization header."""
    lines = [
        RULE,
        f"  API KEY -- {label}",
        RULE,
        f"  {credential}",
        RULE,
        f"  Expires: {expires}",
        "  Send it as a header:",
        f'    curl -H "Authorization: Bearer {credential}" https://your-host.example.com/api/me',
        "  It is not stored anywhere and cannot be shown again. If it leaks,",
        "  revoke it: python -m app manage revoke-key --key-id <id>",
        RULE,
    ]
    print("\n".join(lines), flush=True)
