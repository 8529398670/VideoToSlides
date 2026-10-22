"""The container image's fixed layout, or nothing at all outside it.

Go pins this with a build tag. The Python stand-in is a module that only the
Dockerfile creates: it copies layout_container.py to layout_pinned.py inside
the image, and nowhere else does that file exist (it is in .gitignore). So a
checkout or a venv sees an empty layout and takes every path from config.yaml,
while the image always sees the paths it was built to be mounted into.

These are not settings because they are not choices -- they are the paths
dockerRun.sh mounts into. Pinning them in the image rather than passing them
through ENV or flags means `docker exec <name> python -m app manage ...` finds
the same config and database as the server with nothing extra to type, and a
host-oriented server.app_dir in the shared config.yaml cannot point the
container at a path that is not mounted.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Layout:
    config_file: str = ""
    app_dir: str = ""
    host: str = ""


def _load() -> Layout:
    try:
        from app.core import layout_pinned  # type: ignore[attr-defined]
    except ImportError:
        return Layout()
    return Layout(
        config_file=layout_pinned.CONFIG_FILE,
        app_dir=layout_pinned.APP_DIR,
        host=layout_pinned.HOST,
    )


PINNED = _load()


def in_container() -> bool:
    """Whether this code is running from the container image."""
    return PINNED.config_file != ""
