"""The `health` subcommand: the container's HEALTHCHECK.

It lives in the app rather than as a wget line in the Dockerfile because the
port comes from config.yaml -- a hardcoded URL in the image would silently go
stale the moment someone changed server.port.

It asks /api/health, which touches nothing, so a slow database query can never
get a healthy container killed and restarted.
"""
from __future__ import annotations

import urllib.request

from app.core import config


def run() -> None:
    """Raises unless the local server answers /api/health with 200."""
    file, _, _ = config.current()
    # An empty ProxyHandler: urllib otherwise honours HTTP_PROXY and friends
    # from the environment, and a health probe of 127.0.0.1 must never be
    # sent anywhere else.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://127.0.0.1:{file.server.port}/api/health"
    with opener.open(url, timeout=2) as response:
        if response.status != 200:
            raise RuntimeError(f"health endpoint answered {response.status}")
