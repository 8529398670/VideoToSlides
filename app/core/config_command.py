"""`python -m app config ...` -- create, check, or read config.yaml.

These exist so that nothing else has to parse YAML. dockerRun.sh evals the
output of `config shell docker`, which means the script gets the same
defaults, the same strict decoding, and the same validation as the server --
a typo in the docker section fails the deploy before anything starts, instead
of being ignored by a second, looser parser.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from app.core import config

USAGE = f"""config -- create, check, or read config.yaml

Usage:
  init  [--file ./config.yaml]       write a new config.yaml with a fresh secret
                                     key (--file - prints it instead)
  check [--file path]                validate the config in use and say which
                                     file it is
  shell [--file path] docker         print the docker section as shell
                                     assignments, for dockerRun.sh to eval
  google [--file path] <client.json> copy client_id and client_secret from the
                                     OAuth client JSON Google lets you download
                                     into the google: section

Without --file, the file is found the way the server finds it:
  ./config.yaml, then ~/.config/{config.APP_SLUG}/config.yaml
"""


def run(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="python -m app config", description=USAGE,
                                     formatter_class=argparse.RawDescriptionHelpFormatter, add_help=True)
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="write a new config.yaml with a fresh secret key")
    init.add_argument("--file", default=config.CONFIG_FILE_NAME, help="where to write it; - for stdout")

    check = commands.add_parser("check", help="validate the config in use")
    check.add_argument("--file", default="", help="config file to check (default: the one in use)")

    shell = commands.add_parser("shell", help="print one section as shell assignments")
    shell.add_argument("--file", default="", help="config file to read (default: the one in use)")
    shell.add_argument("section", choices=["docker"])

    google = commands.add_parser("google", help="set the Google OAuth client from its downloaded JSON")
    google.add_argument("--file", default="", help="config file to update (default: the one in use)")
    google.add_argument("client_json", help="the client_secret_....json downloaded from Google Cloud")

    args = parser.parse_args(arguments)
    handlers = {"init": command_init, "check": command_check, "shell": command_shell, "google": command_google}
    handlers[args.command](args)


def command_init(args: argparse.Namespace) -> None:
    if args.file == "-":
        sys.stdout.write(config.new_file_content())
        return
    config.write_new(Path(args.file))
    print(f"wrote {args.file} (mode 0600, fresh secret key)")
    print("Review it, then keep it out of version control and back it up with the app's data.")


def pick_file(explicit: str) -> tuple[config.File, Path, bool]:
    """The --file given, or the file the server would use. found is False when
    there is no file, in which case the defaults apply."""
    if explicit == "":
        return config.current()
    path = Path(explicit)
    if not path.is_file():
        return config.defaults(), path, False
    return config.read_file(path), path, True


def command_check(args: argparse.Namespace) -> None:
    file, path, found = pick_file(args.file)
    if not found:
        if args.file:
            raise config.ConfigError(f"{path} does not exist")
        print(f"no config.yaml yet -- the server will create {path} on first run")
        return
    config.check_secret_key(file, path)
    print(f"ok  {path}")
    warning = config.permission_warning(path)
    if warning:
        print(f"warning: {warning}")


def shell_quote(value: str) -> str:
    """Single-quote a value for POSIX sh, which makes any content inert under
    eval -- on top of validate() having already rejected anything unusual."""
    return "'" + value.replace("'", "'\\''") + "'"


def command_shell(args: argparse.Namespace) -> None:
    file, _, _ = pick_file(args.file)
    docker = file.docker
    # The full -p value is assembled here so the script does not have to know
    # that an IPv6 address needs brackets.
    bind = f"[{docker.bind_address}]" if ":" in docker.bind_address else docker.bind_address
    lines = [
        ("docker_image", docker.image),
        ("docker_container", docker.container),
        ("docker_bind_address", docker.bind_address),
        ("docker_host_port", str(docker.host_port)),
        ("docker_publish", f"{bind}:{docker.host_port}:{file.server.port}"),
        ("docker_data_dir", docker.data_dir),
        ("docker_restart", docker.restart),
        ("docker_memory", docker.memory),
        ("docker_cpus", docker.cpus),
        ("docker_pids_limit", str(docker.pids_limit)),
        ("docker_tmpfs_size", docker.tmpfs_size),
        ("server_port", str(file.server.port)),
        ("server_secure_cookies", "true" if file.server.secure_cookies else "false"),
        ("server_trust_proxy", "true" if file.server.trust_proxy else "false"),
    ]
    # Never the secret key: nothing a script does needs it, and this output
    # tends to end up in terminal scrollback and CI logs.
    for name, value in lines:
        print(f"{name}={shell_quote(value)}")


def command_google(args: argparse.Namespace) -> None:
    """Fill in google.client_id and google.client_secret from the JSON file
    Google Cloud offers when a client is created, so nobody has to copy a
    secret out of a web page by hand. Only those two lines change; every
    comment and other setting in config.yaml stays as it was, and the result
    is validated before it replaces the file."""
    try:
        data = json.loads(Path(args.client_json).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise config.ConfigError(f"could not read {args.client_json}: {error}") from None
    client = (data.get("web") or data.get("installed")) if isinstance(data, dict) else None
    if not isinstance(client, dict) or not client.get("client_id") or not client.get("client_secret"):
        raise config.ConfigError(f"{args.client_json} is not a Google OAuth client file (no client_id/client_secret)")
    if "web" not in data:
        print("warning: this is not a \"Web application\" client -- Google may refuse its redirect URI", file=sys.stderr)

    path = Path(args.file) if args.file else config.locate()[0]
    if not path.is_file():
        raise config.ConfigError(f"{path} does not exist -- create it first (./run.sh creates one)")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.match(r"^google:\s*(#.*)?$", line)), None)
    values = {"client_id": client["client_id"], "client_secret": client["client_secret"]}
    if start is None:
        lines.append("\ngoogle:\n" + "".join(f"  {key}: {json.dumps(value)}\n" for key, value in values.items()))
    else:
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^[^\s#]", lines[i])), len(lines))
        for key, value in values.items():
            line = f"  {key}: {json.dumps(value)}\n"
            found = next((i for i in range(start + 1, end) if re.match(rf"^  {key}:", lines[i])), None)
            if found is None:
                lines.insert(start + 1, line)
                end += 1
            else:
                lines[found] = line
    text = "".join(lines)

    file = config.parse(text, str(path))  # refuse to write anything invalid
    partial = path.with_name(path.name + ".part")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    partial.replace(path)

    redirect = file.google.redirect_url or config.default_google_redirect(file.server.port)
    print(f"google client set in {path}")
    registered = client.get("redirect_uris") or []
    if registered and redirect not in registered:
        print(f"warning: {redirect} is not among this client's redirect URIs ({', '.join(registered)}).")
        print("         Add it in Google Cloud (Clients -> your client), or set google.redirect_url.")
    print("Restart the server to use it.")
