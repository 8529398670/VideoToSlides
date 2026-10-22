"""`python -m app` -- the web server, and the admin CLI.

    python -m app                    run the web server
    python -m app manage <cmd>       account administration
    python -m app config <cmd>       create, check, or read config.yaml
    python -m app worker <job_id>    process one job (started by the server)
    python -m app health             exit 0 if the local server answers
    python -m app version            build information

One entry point for everything, so the server and the tools around it always
read the same config.yaml the same way. Imports happen inside each branch:
`health` runs every 30 seconds in the container and should not pay for
loading the web stack.
"""
from __future__ import annotations

import platform
import sys

PROJECT_NAME = "VideoToSlides"

USAGE = """{name} {version}

Usage:
  python -m app                 run the web server
  python -m app manage <cmd>    account administration
  python -m app config <cmd>    create, check, or read config.yaml
  python -m app worker <job>    process one job (the server starts these)
  python -m app health          exit 0 if the local server answers
  python -m app version         build information
"""


def build_info() -> tuple[str, str, str]:
    """(version, commit, build date). The Dockerfile writes app/build_info.py
    from git; anywhere else these are the defaults, which is a useful signal
    in itself."""
    try:
        from app import build_info  # type: ignore[attr-defined]
    except ImportError:
        return "dev", "unknown", "unknown"
    return build_info.VERSION, build_info.COMMIT, build_info.BUILD_DATE


def fail(message: object) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def main(arguments: list[str]) -> None:
    version, commit, build_date = build_info()
    command, rest = (arguments[0], arguments[1:]) if arguments else ("", [])

    if command == "":
        from app.core.config import ConfigError
        from app.main import serve

        try:
            serve(version)
        except (ConfigError, RuntimeError) as error:
            print(f"[fatal] {error}", file=sys.stderr)
            sys.exit(1)
    elif command == "manage":
        from app import manage

        try:
            manage.run(rest)
        except Exception as error:  # noqa: BLE001 -- every failure is reported the same way
            fail(error)
    elif command == "config":
        from app.core import config_command

        try:
            config_command.run(rest)
        except Exception as error:  # noqa: BLE001
            fail(error)
    elif command == "worker":
        from app.jobs import worker

        worker.run(rest)
    elif command == "health":
        from app.core import healthcheck

        try:
            healthcheck.run()
        except Exception as error:  # noqa: BLE001
            print(f"unhealthy: {error}", file=sys.stderr)
            sys.exit(1)
    elif command in ("version", "--version", "-version"):
        print(
            f"{PROJECT_NAME} {version} (commit {commit}, built {build_date}, "
            f"{sys.platform}/{platform.machine()}, Python {platform.python_version()})"
        )
    elif command in ("help", "-h", "--help"):
        from app import manage
        from app.core import config_command

        print(USAGE.format(name=PROJECT_NAME, version=version))
        print(manage.USAGE)
        print(config_command.USAGE)
    else:
        print(USAGE.format(name=PROJECT_NAME, version=version), file=sys.stderr)
        fail(f"unknown command {command!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
