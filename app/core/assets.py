"""Decides where the frontend comes from: a configured location, or the copy
that ships with the code.

This is the Python counterpart of the Go template's "disk or embedded"
choice. There is no embedding here -- the code always has its own static/ and
language.yaml beside the app/ package -- but the rule is the same:

  - server.static_dir is used when that directory exists. It defaults to
    ./static beside config.yaml, which in a source checkout *is* the code's
    copy, so editing a file and refreshing just works.
  - Run with a config.yaml somewhere else (~/.config/<slug>/), there is no
    static/ beside it, and the bundled copy is used.

The static tree and the language file are decided independently on purpose:
dropping a language.yaml into the app directory rewords the UI while the
frontend itself stays the shipped one. That is the supported way for an
operator to translate or soften a deployment's copy without touching code.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The directory holding app/, static/, and language.yaml.
CODE_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_STATIC = CODE_ROOT / "static"
BUNDLED_LANGUAGE = CODE_ROOT / "language.yaml"


@dataclass(frozen=True)
class Bundle:
    static_dir: Path
    language_yaml: bytes
    # Where the above came from, for the startup log. Seeing which copy is in
    # use is the fastest way to explain why an edit did or did not show up.
    source: str


def resolve(static_dir: str, language_file: str) -> Bundle:
    if static_dir and Path(static_dir).is_dir():
        static_path = Path(static_dir).resolve()
        static_source = f"{static_path} (server.static_dir)"
    else:
        static_path = BUNDLED_STATIC
        static_source = f"{static_path} (bundled)"
    if not static_path.is_dir():
        raise RuntimeError(f"no static directory at {static_dir!r} and none bundled at {BUNDLED_STATIC}")

    if language_file and Path(language_file).is_file():
        language_path = Path(language_file).resolve()
        language_source = f"{language_path}"
    else:
        language_path = BUNDLED_LANGUAGE
        language_source = f"{language_path} (bundled)"
    try:
        language_yaml = language_path.read_bytes()
    except OSError as error:
        raise RuntimeError(f"could not read language file {str(language_path)!r}: {error.strerror}") from None
    if not language_yaml.strip():
        raise RuntimeError(f"{language_path} is empty -- the UI would render with no text")

    return Bundle(
        static_dir=static_path,
        language_yaml=language_yaml,
        source=f"static={static_source} language={language_source}",
    )
