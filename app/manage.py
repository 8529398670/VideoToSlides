"""`python -m app manage ...` -- the handful of account operations that cannot
happen through the web UI, because they are needed before anyone can log in or
at the point when nobody can.

It opens the database directly, whether or not the server is running. SQLite
in WAL mode allows that: readers never block, and a write here waits its turn
behind the server's (up to the busy timeout) rather than failing. That is why
this template has no control socket, unlike the Go one -- bolt hands its file
to a single process, SQLite does not.

Every command goes through the same model functions the web routes use, so the
rules hold whichever door a change comes through: disabling an account kills
its sessions and unused login links, and an API key can never outrank the
account that owns it.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from app.core import bootstrap, config, db, settings
from app.jobs import files
from app.pipeline import runner
from app.models import api_keys, credentials, jobs, login_tokens, users
from app.models import settings as settings_model

USAGE = """manage -- account administration for VideoToSlides

Usage:
  bootstrap-admin --name "Ada"          create the first admin + login link
  create-login    --name "Grace" [--role user]
  reissue-login   --user-id 3           fresh link for an existing user
  list-users                            id, name, role, status
  set-disabled    --user-id 3 [--enable]
  revoke-access   --user-id 3           end every session, login link and API key
  list-keys       [--user-id 3]         API keys, whose they are, when last used
  create-key      --user-id 3 --name "CI" [--role user] [--days 90]
  revoke-key      --key-id abc123       kill one API key now
  list-jobs       [--user-id 3]         video jobs: status, slides, where the images are
  requeue-job     --job-id 12           run a job again (finished, failed or cancelled)
  detect-builds   --job-id 12           re-mark incremental build steps on a finished
                                        job, after tuning app/pipeline/tunables.py
  link-playlist   --url URL [--user-id 3]
                                        record a playlist's link and order on videos
                                        added before decks carried them
  paths                                 where this app keeps its state

An API key carries a role from the same set users do, so an admin-scoped key
can do what an admin can and a user-scoped one cannot. A key never outranks
the account it belongs to. --days 0 means it never expires; omit --days to use
the server's configured default.

These work whether or not the server is running: the database is opened
directly, which SQLite's WAL mode makes safe alongside a live server.

Every command reads the same config.yaml as the server (see "paths" for which
one). Its server.secret_key in particular must be the one the data was written
with, or the stored records cannot be decrypted.
"""


class ManageError(Exception):
    """A command that cannot do what was asked. Shown as `error: <message>`."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app manage", description=USAGE,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = commands.add_parser("bootstrap-admin", help="create the first admin + login link")
    p.add_argument("--name", required=True, help="display name for the first admin")
    p.add_argument("--force", action="store_true", help="create another admin even if one exists")

    p = commands.add_parser("create-login", help="create a user and print a login link")
    p.add_argument("--name", required=True, help="display name")
    p.add_argument("--role", choices=[users.ROLE_USER, users.ROLE_ADMIN], default=users.ROLE_USER)

    p = commands.add_parser("reissue-login", help="fresh login link for an existing user")
    p.add_argument("--user-id", type=int, required=True)

    commands.add_parser("list-users", help="id, name, role, status")

    p = commands.add_parser("set-disabled", help="disable (or re-enable) an account")
    p.add_argument("--user-id", type=int, required=True)
    p.add_argument("--enable", action="store_true", help="re-enable instead of disabling")

    p = commands.add_parser("revoke-access", help="end every session, login link and API key")
    p.add_argument("--user-id", type=int, required=True)

    p = commands.add_parser("list-keys", help="API keys, whose they are, when last used")
    p.add_argument("--user-id", type=int, default=None, help="only this user's keys (default: everyone's)")

    p = commands.add_parser("create-key", help="mint an API key for an account")
    p.add_argument("--user-id", type=int, required=True, help="id of the account the key belongs to")
    p.add_argument("--name", required=True, help='what this key is for, e.g. "deploy bot"')
    p.add_argument("--role", choices=[users.ROLE_USER, users.ROLE_ADMIN], default=users.ROLE_USER,
                   help="never more than the account has")
    p.add_argument("--days", type=int, default=None,
                   help="lifetime in days; 0 never expires; omit for the server default")

    p = commands.add_parser("revoke-key", help="kill one API key now")
    p.add_argument("--key-id", required=True, help="id of the key to revoke (from list-keys)")

    p = commands.add_parser("list-jobs", help="video jobs: status, slides, where the images are")
    p.add_argument("--user-id", type=int, default=None, help="only this user's jobs (default: everyone's)")

    p = commands.add_parser("requeue-job", help="run a job again (finished, failed or cancelled)")
    p.add_argument("--job-id", type=int, required=True)

    p = commands.add_parser("detect-builds", help="re-mark incremental build steps on a finished job")
    p.add_argument("--job-id", type=int, required=True)

    p = commands.add_parser("link-playlist", help="record a playlist's link and order on existing videos")
    p.add_argument("--url", required=True, help="the playlist (or channel tab) URL")
    p.add_argument("--user-id", type=int, default=None, help="only this user's jobs (default: everyone's)")

    commands.add_parser("paths", help="where this app keeps its state")
    return parser


def run(arguments: list[str]) -> None:
    args = build_parser().parse_args(arguments)
    try:
        cfg = config.load()
    except config.ConfigError as error:
        raise ManageError(f"config error: {error}") from None

    # "paths" reports configuration only, so it never needs the database and
    # stays useful when everything else is failing.
    if args.command == "paths":
        command_paths(cfg)
        return

    store = db.open_store(cfg.database_path, cfg.secret_key, cfg.encrypt_at_rest)
    # Say where this went. stderr, so piping list-users stays clean.
    print(f"[manage] database at {cfg.database_path}", file=sys.stderr)

    handlers = {
        "bootstrap-admin": command_bootstrap_admin,
        "create-login": command_create_login,
        "reissue-login": command_reissue_login,
        "list-users": command_list_users,
        "set-disabled": command_set_disabled,
        "revoke-access": command_revoke_access,
        "list-keys": command_list_keys,
        "create-key": command_create_key,
        "revoke-key": command_revoke_key,
        "list-jobs": command_list_jobs,
        "requeue-job": command_requeue_job,
        "detect-builds": command_detect_builds,
        "link-playlist": command_link_playlist,
    }
    try:
        handlers[args.command](store, cfg, args)
    except db.DecodeError as error:
        raise ManageError(str(error)) from None


def require_user(store: db.Store, user_id: int) -> users.User:
    user = users.get_user(store, user_id)
    if user is None:
        raise ManageError(f"no user with id {user_id}")
    return user


def command_bootstrap_admin(store: db.Store, cfg: config.Config, args: argparse.Namespace) -> None:
    if not users.valid_display_name(args.name):
        raise ManageError("--name must be 1-80 characters")
    if users.any_admin_exists(store) and not args.force:
        print("An admin already exists. Re-run with --force to create another.")
        return
    user = users.create_user(store, args.name, users.ROLE_ADMIN)
    credential = login_tokens.issue_login_token(store, user.id, cfg.login_token_ttl_seconds)
    bootstrap.print_login_link(cfg, f"admin #{user.id} {user.display_name!r}", credential)


def command_create_login(store: db.Store, cfg: config.Config, args: argparse.Namespace) -> None:
    if not users.valid_display_name(args.name):
        raise ManageError("--name must be 1-80 characters")
    user = users.create_user(store, args.name, args.role)
    credential = login_tokens.issue_login_token(store, user.id, cfg.login_token_ttl_seconds)
    bootstrap.print_login_link(cfg, f"{user.role} #{user.id} {user.display_name!r}", credential)


def command_reissue_login(store: db.Store, cfg: config.Config, args: argparse.Namespace) -> None:
    user = require_user(store, args.user_id)
    credential = login_tokens.issue_login_token(store, user.id, cfg.login_token_ttl_seconds)
    bootstrap.print_login_link(cfg, f"{user.role} #{user.id} {user.display_name!r}", credential)


def command_list_users(store: db.Store, cfg: config.Config, args: argparse.Namespace) -> None:
    rows = users.list_users(store)
    if not rows:
        print('No users yet. Create the first admin with: python -m app manage bootstrap-admin --name "Your Name"')
        return
    print(f"{'ID':<6} {'NAME':<30} {'ROLE':<8} STATUS")
    for user in rows:
        print(f"{user.id:<6} {user.display_name:<30} {user.role:<8} {'disabled' if user.disabled else 'active'}")


def command_set_disabled(store: db.Store, cfg: config.Config, args: argparse.Namespace) -> None:
    user = require_user(store, args.user_id)
    disabled = not args.enable
    # Disabling deletes the account's sessions and unused login links in the
    # same transaction -- see credentials.set_user_disabled.
    credentials.set_user_disabled(store, user.id, disabled)
    print(f"user #{user.id} ({user.display_name}) disabled={str(disabled).lower()}")


def command_revoke_access(store: db.Store, cfg: config.Config, args: argparse.Namespace) -> None:
    """The incident-response command: run it when an account's credentials
    may be in someone else's hands. The account stays enabled; follow it with
    reissue-login to let its owner back in."""
    user = require_user(store, args.user_id)
    revoked = credentials.revoke_all_credentials(store, user.id)
    if revoked is None:
        raise ManageError(f"no user with id {user.id}")
    print(
        f"user #{user.id} ({user.display_name}): ended {revoked.sessions} sessions, deleted "
        f"{revoked.login_links} unused login links, revoked {revoked.api_keys} API keys"
    )
    print(f"To let them back in: python -m app manage reissue-login --user-id {user.id}")


def format_time(value: datetime | None, absent: str = "never") -> str:
    """Minute precision keeps the listings narrow -- enough for "when was this
    last used", and a full timestamp per column would wrap any normal
    terminal."""
    return absent if value is None else value.astimezone().strftime("%Y-%m-%d %H:%M")


def key_status(key: api_keys.APIKey) -> str:
    if key.revoked:
        return "revoked"
    if key.expired:
        return "expired"
    return "active"


def command_list_keys(store: db.Store, cfg: config.Config, args: argparse.Namespace) -> None:
    keys = (
        api_keys.list_api_keys(store)
        if args.user_id is None
        else api_keys.list_api_keys_for_user(store, args.user_id)
    )
    if not keys:
        print('No API keys. Create one with: python -m app manage create-key --user-id <id> --name "CI"')
        return
    names = {user.id: user.display_name for user in users.list_users(store)}
    row = "{:<18} {:<22} {:<6} {:<20} {:<8} {:<16} {}"
    print(row.format("KEY ID", "NAME", "ROLE", "OWNER", "STATUS", "LAST USED", "EXPIRES"))
    for key in keys:
        print(row.format(
            key.id, key.name, key.role, names.get(key.user_id, f"#{key.user_id}"), key_status(key),
            format_time(key.last_used_at), format_time(key.expires_at),
        ))


def command_create_key(store: db.Store, cfg: config.Config, args: argparse.Namespace) -> None:
    """How a CI system or a cron job gets a credential without anyone signing
    in. --role is what "mirroring admin and non-admin permissions" means in
    practice: --role admin produces a key that can do what an admin can, and
    the default produces one that cannot."""
    # Refuse rather than mint something that cannot be used: with the feature
    # off the server ignores the Authorization header entirely.
    if not cfg.api_keys_enabled:
        raise ManageError("api keys are disabled on this server (auth.api_keys: false)")
    if not api_keys.valid_name(args.name):
        raise ManageError("--name must be 1-80 characters")
    owner = require_user(store, args.user_id)
    ttl_seconds = api_keys.ttl_from_days(args.days, cfg.api_key_ttl_seconds)
    if ttl_seconds is None:
        raise ManageError(f"--days must be 0 (never) or up to {api_keys.API_KEY_MAX_DAYS}")
    try:
        credential, key = api_keys.issue_api_key(store, owner.id, args.name, args.role, ttl_seconds)
    except api_keys.APIKeyError as error:
        raise ManageError(str(error)) from None
    bootstrap.print_api_key(
        f"{key.role}-scoped {key.name!r} for #{owner.id} {owner.display_name!r}",
        credential,
        format_time(key.expires_at),
    )


def command_revoke_key(store: db.Store, cfg: config.Config, args: argparse.Namespace) -> None:
    key = api_keys.revoke_api_key(store, args.key_id)
    if key is None:
        raise ManageError(f"no api key with id {args.key_id}")
    print(f"api key {key.id} ({key.name!r}, owner #{key.user_id}) revoked -- it stops working immediately")


def command_list_jobs(store: db.Store, cfg: config.Config, args: argparse.Namespace) -> None:
    found = jobs.list_jobs(store)
    if args.user_id is not None:
        found = [job for job in found if job.user_id == args.user_id]
    if not found:
        print("No jobs.")
        return
    row = "{:<6} {:<6} {:<11} {:>6}  {:<40} {}"
    print(row.format("JOB", "OWNER", "STATUS", "SLIDES", "TITLE", "IMAGES"))
    for job in found:
        images = files.slides_dir(cfg, job.id) if job.slides else ""
        print(row.format(job.id, f"#{job.user_id}", job.status, len(job.slides), job.title[:40], images))


def command_requeue_job(store: db.Store, cfg: config.Config, args: argparse.Namespace) -> None:
    """The running server's dispatcher picks it up within a few seconds."""
    job = jobs.retry_job(store, args.job_id)
    if job is None:
        raise ManageError(f"job {args.job_id} does not exist or is still running")
    print(f"job {job.id} queued again")


def command_detect_builds(store: db.Store, cfg: config.Config, args: argparse.Namespace) -> None:
    """Re-run only the build-step stage on a finished job's slide images, using
    the motion map its scan saved. For checking a change to the BUILD_*
    tunables without reprocessing the video. Jobs made before motion maps
    existed have none; run those again instead (requeue-job, or Run again)."""
    job = jobs.get_job(store, args.job_id)
    if job is None or job.status != jobs.STATUS_READY:
        raise ManageError(f"job {args.job_id} does not exist or is not finished")
    settings.apply_tunables(settings_model.get_overrides(store))
    directory = files.job_dir(cfg, job.id)
    motion = runner.load_motion(directory)
    if motion is None:
        raise ManageError(f"job {job.id} has no motion map (made before build detection) -- run it again: "
                          f"python -m app manage requeue-job --job-id {job.id}")
    slides = [
        runner.Slide(n=s["n"], file=s["file"], start_sec=s["start_sec"], captured_sec=s["captured_sec"],
                     energy=s.get("energy", 0.0))
        for s in job.slides
    ]
    runner.mark_build_steps(slides, directory / runner.SLIDES_DIR, motion, log=print)
    updated = jobs.set_build_steps(store, job.id, [s.build_step for s in slides], [s.start_sec for s in slides])
    if updated is None:
        raise ManageError(f"job {job.id} changed while this ran -- try again")
    kept = [s.n for s in slides if not s.build_step]
    print(f"job {job.id}: {len(slides) - len(kept)} build step(s); deck slides: {kept}")


def command_link_playlist(store: db.Store, cfg: config.Config, args: argparse.Namespace) -> None:
    """Videos added before jobs kept their playlist have no playlist link or
    position for the deck's notes. This reads the playlist from YouTube and
    fills both in on every job whose video is in it -- matched by video id,
    so nothing is downloaded or re-run."""
    from app.sources import youtube

    url = youtube.normalize_url(args.url)
    if url is None:
        raise ManageError("that is not a YouTube URL")
    try:
        playlist, entries = youtube.expand(url, cfg.youtube_max_playlist_items)
    except youtube.YouTubeError as failure:
        raise ManageError(str(failure)) from None
    if playlist is None:
        raise ManageError("that URL is a single video, not a playlist")
    by_video = {entry.video_id: entry for entry in entries if entry.video_id}
    linked = 0
    for job in jobs.list_jobs(store):
        if args.user_id is not None and job.user_id != args.user_id:
            continue
        entry = by_video.get(youtube.video_id(job.source_url))
        if entry is not None and jobs.set_playlist(store, job.id, entry.playlist_info(playlist)) is not None:
            linked += 1
            print(f"job {job.id}: #{entry.index} {job.title[:60]}")
    print(f"linked {linked} job(s) to {playlist.title!r} ({len(entries)} videos in it); convert again to update decks")


def command_paths(cfg: config.Config) -> None:
    """Answers "where did it put my data", which is otherwise a question you
    can only settle by reading the source or the startup log."""

    def note(path: str) -> str:
        return "" if Path(path).exists() else "  (absent -- optional)"

    print(f"{'config':<14} {cfg.config_file}  (holds the secret key)")
    print(f"{'app dir':<14} {cfg.app_dir}")
    print(f"{'database':<14} {cfg.database_path}  (plus -wal and -shm files while in use)")
    print(f"{'storage':<14} {cfg.storage_dir}")
    print(f"{'language':<14} {cfg.language_file}{note(cfg.language_file)}")
    if cfg.config_warning:
        print(f"{'warning':<14} {cfg.config_warning}")

    # Whether the key sits inside the app dir decides what a backup has to
    # cover, so say which case this is rather than leaving it to be worked out
    # from the paths above.
    print("")
    print("Change any of these in config.yaml (server.app_dir moves the app dir).")
    if cfg.config_file.parent == cfg.app_dir:
        print("Back up the app dir as a unit -- app.db is unreadable without the key in config.yaml.")
    else:
        print("config.yaml is NOT in the app dir. Back up both; app.db is unreadable without")
        print("the key in config.yaml, and either one alone is useless.")
    print("Stop the server before copying app.db, or the copy can miss writes still in app.db-wal.")
