"""Settings an admin can change from the browser (the gear icon), layered on
top of the files that define their defaults:

    config.yaml / app/core/defaults.yaml   server-side defaults (jobs, uploads,
                                           YouTube, the Google OAuth client)
    app/pipeline/tunables.py               slide-detection defaults
    the database ("settings" bucket)       what an admin changed -- wins

Overrides live in the database rather than in config.yaml because the server
cannot rewrite its own config (in the container it is mounted read-only),
and because they are app state an admin manages, not deployment wiring.
What needs a restart to change -- ports, the secret key, cookie and proxy
settings, Docker limits, the upload chunk size -- stays in config.yaml only.

FIELDS is the single list of what is editable: its type, bounds, and where
the value lands. The API, the validation, and the page are all built from it,
so adding a setting is one entry here plus its label in language.yaml.

Applying is cheap and immediate. The server applies overrides at startup and
again on every save (tunables are module attributes the pipeline reads at
call time; config values replace the Config the routes and dispatcher read).
Each worker process applies them when it starts, so a change affects every
job started after it.
"""
from __future__ import annotations

import dataclasses
import math
import os
from dataclasses import dataclass
from typing import Any

from app.core import config as config_module
from app.core.config import Config
from app.pipeline import tunables as T

GB = 1024 ** 3


class SettingsError(Exception):
    """An invalid value. The message is safe to show."""


@dataclass(frozen=True)
class Field:
    key: str            # API and language.yaml key (settings.fields.<key>)
    group: str          # settings.groups.<group>
    kind: str           # "tunable" (tunables.py) or "config" (Config field)
    target: str         # attribute name in tunables.py or on Config
    type: str           # int | float | bool | str | secret | choice
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    scale: float = 1.0  # stored value = shown value * scale (GB -> bytes)
    choices: tuple[int, ...] = ()


def _t(key: str, group: str, target: str, type_: str, lo: float | None = None, hi: float | None = None,
       step: float | None = None) -> Field:
    return Field(key, group, "tunable", target, type_, lo, hi, step)


def _c(key: str, group: str, target: str, type_: str, lo: float | None = None, hi: float | None = None,
       step: float | None = None, **extra: Any) -> Field:
    return Field(key, group, "config", target, type_, lo, hi, step, **extra)


FIELDS: tuple[Field, ...] = (
    # -- processing (config.yaml jobs/uploads/youtube) --
    _c("max_concurrent", "processing", "jobs_max_concurrent", "int", 1, 16, 1),
    _c("scan_workers", "processing", "jobs_scan_workers", "int", 0, 64, 1),
    _c("extract_workers", "processing", "jobs_extract_workers", "int", 1, 32, 1),
    _c("keep_source", "processing", "jobs_keep_source", "bool"),
    _c("upload_max_gb", "processing", "upload_max_bytes", "float", 0.1, 1000, 0.1, scale=GB),
    _c("youtube_enabled", "processing", "youtube_enabled", "bool"),
    _c("youtube_max_height", "processing", "youtube_max_height", "choice", choices=(360, 480, 720, 1080, 1440, 2160)),
    _c("youtube_max_playlist_items", "processing", "youtube_max_playlist_items", "int", 1, 5000, 1),
    # -- Google Slides (config.yaml google) --
    _c("google_client_id", "google", "google_client_id", "str"),
    _c("google_client_secret", "google", "google_client_secret", "secret"),
    _c("google_redirect_url", "google", "google_redirect_url", "str"),
    # -- detection: the coarse scan --
    _t("stride", "scan", "STRIDE", "int", 1, 30, 1),
    _t("coarse_diff_thresh", "scan", "COARSE_DIFF_THRESH", "float", 0.5, 50, 0.1),
    _t("gradual_window_sec", "scan", "GRADUAL_WINDOW_SEC", "float", 0.5, 10, 0.1),
    _t("gradual_cell_thresh", "scan", "GRADUAL_CELL_THRESH", "float", 1, 100, 1),
    _t("gradual_min_area", "scan", "GRADUAL_MIN_AREA", "float", 0, 1, 0.01),
    _t("min_gap_sec", "scan", "MIN_GAP_SEC", "float", 0, 10, 0.1),
    _t("min_slide_sec", "scan", "MIN_SLIDE_SEC", "float", 0, 30, 0.5),
    # -- detection: capture timing --
    _t("capture_pre_cut_sec", "capture", "CAPTURE_PRE_CUT_SEC", "float", 0, 10, 0.1),
    _t("capture_min_after_cut_sec", "capture", "CAPTURE_MIN_AFTER_CUT_SEC", "float", 0, 10, 0.1),
    # -- detection: talking-head suppression --
    _t("noise_window_sec", "noise", "NOISE_WINDOW_SEC", "float", 1, 300, 1),
    _t("noise_min_rate", "noise", "NOISE_MIN_RATE", "int", 2, 100, 1),
    _t("noise_merge_gap_sec", "noise", "NOISE_MERGE_GAP_SEC", "float", 0, 600, 1),
    _t("noise_min_run_duration_sec", "noise", "NOISE_MIN_RUN_DURATION_SEC", "float", 0, 600, 1),
    _t("noise_min_run_len", "noise", "NOISE_MIN_RUN_LEN", "int", 1, 1000, 1),
    _t("run_exit_quiet_sec", "noise", "RUN_EXIT_QUIET_SEC", "float", 0, 600, 1),
    # -- detection: duplicates --
    _t("hash_size", "duplicates", "HASH_SIZE", "int", 4, 64, 1),
    _t("hash_dist_thresh", "duplicates", "HASH_DIST_THRESH", "int", 0, 4096, 1),
    _t("grid", "duplicates", "GRID", "int", 4, 64, 1),
    _t("region_cell_thresh", "duplicates", "REGION_CELL_THRESH", "float", 0, 255, 1),
    _t("region_min_diff", "duplicates", "REGION_MIN_DIFF", "float", 0, 1, 0.001),
    # -- detection: incremental builds --
    _t("build_merge_enabled", "builds", "BUILD_MERGE_ENABLED", "bool"),
    _t("motion_pixel_thresh", "builds", "MOTION_PIXEL_THRESH", "float", 1, 100, 1),
    _t("motion_min_fraction", "builds", "MOTION_MIN_FRACTION", "float", 0.01, 1, 0.01),
    _t("build_width", "builds", "BUILD_WIDTH", "int", 160, 1920, 16),
    _t("build_bg_tolerance", "builds", "BUILD_BG_TOLERANCE", "float", 0, 255, 1),
    _t("build_ink_thresh", "builds", "BUILD_INK_THRESH", "float", 1, 255, 1),
    _t("build_cell", "builds", "BUILD_CELL", "int", 4, 128, 1),
    _t("build_cell_min_pixels", "builds", "BUILD_CELL_MIN_PIXELS", "int", 1, 1000, 1),
    _t("build_min_added_cells", "builds", "BUILD_MIN_ADDED_CELLS", "int", 1, 1000, 1),
    _t("build_max_loss_cells", "builds", "BUILD_MAX_LOSS_CELLS", "int", 0, 1000, 1),
    _t("build_max_loss_ratio", "builds", "BUILD_MAX_LOSS_RATIO", "float", 0, 1, 0.01),
    # -- output --
    _t("slide_width_in", "output", "SLIDE_WIDTH_IN", "float", 4, 30, 0.1),
    _t("thumb_width", "output", "THUMB_WIDTH", "int", 80, 800, 10),
    _t("jpeg_quality", "output", "JPEG_QUALITY", "int", 1, 31, 1),
)
BY_KEY = {field.key: field for field in FIELDS}
GROUPS = ("processing", "google", "scan", "capture", "noise", "duplicates", "builds", "output")

# The shipped tunables, captured before anything is overridden. Every apply
# starts from these, so removing an override really restores the default.
TUNABLE_DEFAULTS = {field.target: getattr(T, field.target) for field in FIELDS if field.kind == "tunable"}

STR_MAX = 500


def _auto_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


def coerce(field: Field, raw: Any) -> Any:
    """A submitted value, checked and converted to what is stored. Raises
    SettingsError with a message naming the problem."""
    if field.type == "bool":
        if not isinstance(raw, bool):
            raise SettingsError(f"{field.key} must be true or false")
        return raw
    if field.type in ("str", "secret"):
        if not isinstance(raw, str) or len(raw) > STR_MAX:
            raise SettingsError(f"{field.key} must be text of at most {STR_MAX} characters")
        return raw.strip()
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise SettingsError(f"{field.key} must be a number")
    try:
        number = float(raw)
    except ValueError:
        raise SettingsError(f"{field.key} must be a number") from None
    if not math.isfinite(number):
        raise SettingsError(f"{field.key} must be a number")
    if field.type == "choice":
        if int(number) not in field.choices:
            raise SettingsError(f"{field.key} must be one of {', '.join(map(str, field.choices))}")
        return int(number)
    if field.minimum is not None and number < field.minimum:
        raise SettingsError(f"{field.key} must be at least {field.minimum:g}")
    if field.maximum is not None and number > field.maximum:
        raise SettingsError(f"{field.key} must be at most {field.maximum:g}")
    if field.type == "int":
        if number != int(number):
            raise SettingsError(f"{field.key} must be a whole number")
        return int(number)
    return number * field.scale if field.scale != 1.0 else number


def apply_tunables(overrides: dict[str, Any]) -> None:
    for field in FIELDS:
        if field.kind == "tunable":
            setattr(T, field.target, overrides.get(field.key, TUNABLE_DEFAULTS[field.target]))


def apply_config(base: Config, overrides: dict[str, Any]) -> Config:
    changes: dict[str, Any] = {}
    for field in FIELDS:
        if field.kind != "config" or field.key not in overrides:
            continue
        value = overrides[field.key]
        if field.target == "jobs_scan_workers" and value == 0:
            value = _auto_workers()
        if field.target == "google_redirect_url" and value == "":
            value = config_module.default_google_redirect(base.port)
        if field.target == "upload_max_bytes":
            value = int(value)
        changes[field.target] = value
    return dataclasses.replace(base, **changes) if changes else base


def check(effective: Config) -> None:
    """Rules that span fields, checked on the result before saving."""
    if effective.google_client_id and not effective.google_client_secret:
        raise SettingsError("the Google client secret is required when a client ID is set")
    url = effective.google_redirect_url
    if not (url.startswith(("http://", "https://")) and url.rstrip("/").endswith("/api/google/callback")):
        raise SettingsError("the Google redirect URL must be the full http(s) address of /api/google/callback")


def describe(base: Config, overrides: dict[str, Any]) -> dict[str, Any]:
    """Everything the settings page needs, in display units. Secrets are
    never sent back -- only whether one is set."""
    effective = apply_config(base, overrides)
    groups: dict[str, list[dict[str, Any]]] = {group: [] for group in GROUPS}
    for field in FIELDS:
        if field.kind == "tunable":
            default = TUNABLE_DEFAULTS[field.target]
            value = overrides.get(field.key, default)
        else:
            default = getattr(base, field.target)
            value = getattr(effective, field.target)
        if field.scale != 1.0:
            default, value = round(default / field.scale, 3), round(value / field.scale, 3)
        entry: dict[str, Any] = {
            "key": field.key,
            "type": field.type,
            "overridden": field.key in overrides,
            "min": field.minimum,
            "max": field.maximum,
            "step": field.step,
            "choices": list(field.choices),
        }
        if field.type == "secret":
            entry.update(value="", default="", set=bool(value))
        else:
            entry.update(value=value, default=default)
        groups[field.group].append(entry)
    return {
        "groups": [{"id": group, "fields": groups[group]} for group in GROUPS],
        "google_redirect_default": config_module.default_google_redirect(base.port),
        "google_redirect_effective": effective.google_redirect_url,
    }


def merged(overrides: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Overrides after applying a page's changes. null resets a field to its
    default; an empty secret means "unchanged" (the page never has it)."""
    if not isinstance(changes, dict):
        raise SettingsError("changes must be an object")
    result = dict(overrides)
    for key, raw in changes.items():
        field = BY_KEY.get(key)
        if field is None:
            raise SettingsError(f"unknown setting {key!r}")
        if raw is None:
            result.pop(key, None)
            continue
        if field.type == "secret" and raw == "":
            continue
        result[key] = coerce(field, raw)
    return result
