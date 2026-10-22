"""The container image's fixed layout. The Dockerfile copies this file to
layout_pinned.py, which is what makes it apply -- see layout.py. Nothing
imports this module directly.

    /app/config.yaml   ./config.yaml, mounted read-only
    /app/data          docker.data_dir, the only writable mount
    0.0.0.0            inside the container's own network namespace; what is
                       reachable from outside is docker.bind_address
"""
CONFIG_FILE = "/app/config.yaml"
APP_DIR = "/app/data"
HOST = "0.0.0.0"
