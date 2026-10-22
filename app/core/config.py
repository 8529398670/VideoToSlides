"""The single source of truth for everything this app can be told to do
differently: the server and the Docker container. All of it lives in one
config.yaml.

Nothing reads environment variables. A setting that could come from the
environment, a file, or a default has to be hunted for in three places; one
that can only come from config.yaml is answered by reading one file. The
shell scripts follow the same rule -- they ask this module for their values
(`python -m app config shell docker`, see config_command.py) rather than
parsing YAML themselves, so there is exactly one parser and one set of
validation rules.

Defaults are not written in Python. They live in defaults.yaml, which ships
beside this file, documents every key, and is what `config init` writes out.
A config.yaml is decoded on top of it, strictly: an unknown key is an error,
because a typo that is silently ignored is a setting that silently is not
applied.
"""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from app.core.layout import PINNED, in_container

# APP_SLUG names the application on disk. It is what turns into
# ~/.config/<slug>/, so it is deliberately the lowercase, filesystem-safe form
# of the project name rather than the display name. It is a constant rather
# than a setting because it is how config.yaml itself is found.
APP_SLUG = "videotoslides"

# Filenames this app uses. Everything it persists lives under the app
# directory -- see default_app_dir for why.
CONFIG_FILE_NAME = "config.yaml"
DATABASE_FILE_NAME = "app.db"
STORAGE_DIR_NAME = "storage"
LANGUAGE_FILE_NAME = "language.yaml"

DEFAULTS_PATH = Path(__file__).with_name("defaults.yaml")


class ConfigError(Exception):
    """config.yaml is missing, malformed, or holds a value that would fail
    later in a confusing way. The message is meant to be shown as-is."""


# ---------------------------------------------------------------------------
# config.yaml, section by section
# ---------------------------------------------------------------------------
#
# The dataclasses below are the schema. Every key in defaults.yaml must be a
# field here and every field must be in defaults.yaml, so the two cannot drift
# silently. The docker section is decoded and validated too even though the
# server never reads it: that is what lets dockerRun.sh fail on a typo before
# it starts anything.


@dataclass
class AppSection:
    name: str


@dataclass
class ServerSection:
    host: str
    port: int
    app_dir: str
    static_dir: str
    language_file: str
    secret_key: str
    encrypt_at_rest: bool
    secure_cookies: bool
    cookie_name: str
    trust_proxy: bool
    trusted_proxies: list[str]
    proxy_header: str
    body_limit_bytes: int
    rate_limit_max: int
    rate_limit_window_seconds: int


@dataclass
class AuthSection:
    session_ttl_seconds: int
    login_token_ttl_seconds: int
    api_keys: bool
    api_key_ttl_seconds: int


@dataclass
class JobsSection:
    max_concurrent: int
    scan_workers: int
    extract_workers: int
    keep_source: bool


@dataclass
class UploadsSection:
    max_bytes: int
    chunk_bytes: int


@dataclass
class YoutubeSection:
    enabled: bool
    max_height: int
    max_playlist_items: int


@dataclass
class GoogleSection:
    client_id: str
    client_secret: str
    redirect_url: str


@dataclass
class DockerSection:
    image: str
    container: str
    bind_address: str
    host_port: int
    data_dir: str
    restart: str
    memory: str
    cpus: str
    pids_limit: int
    tmpfs_size: str


@dataclass
class File:
    app: AppSection
    server: ServerSection
    auth: AuthSection
    jobs: JobsSection
    uploads: UploadsSection
    youtube: YoutubeSection
    google: GoogleSection
    docker: DockerSection


SECTIONS: dict[str, type] = {
    "app": AppSection,
    "server": ServerSection,
    "auth": AuthSection,
    "jobs": JobsSection,
    "uploads": UploadsSection,
    "youtube": YoutubeSection,
    "google": GoogleSection,
    "docker": DockerSection,
}


# ---------------------------------------------------------------------------
# the resolved configuration the server runs on
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """config.yaml after resolution: paths made absolute, container overrides
    applied. The rest of the app reads this, never File, so a change to the
    file's shape stays inside this module."""

    project_name: str

    # The config.yaml this was loaded from. It also holds the secret key,
    # which is why it is reported wherever backups are mentioned.
    config_file: Path
    # True when this run wrote config_file because none existed. The startup
    # log says so, since a fresh key was generated.
    config_created: bool
    # Non-empty when config_file is readable by other users. A warning rather
    # than an error so a misconfigured mode never stops a server that was
    # working, but it does get logged on every start.
    config_warning: str

    # The one directory this application owns: the database and any uploaded
    # or generated files. Defaulting to ~/.config/<slug> rather than ./data
    # means the app finds the same state whatever directory it is started
    # from.
    app_dir: Path
    database_path: Path
    # For application files: jobs/<id>/ holds each video's slides and decks,
    # uploads/ holds videos still arriving, tmp/ is scratch space.
    storage_dir: Path

    # Used when they exist; otherwise the copies shipped with the code (see
    # app/core/assets.py). Empty means "not configured".
    static_dir: str
    language_file: str

    host: str
    port: int

    # Must stay true anywhere a real browser talks to this over the internet.
    # Only false for plain-http://localhost work, because a Secure cookie is
    # silently dropped over http and the login flow would appear to succeed
    # and then do nothing.
    secure_cookies: bool
    cookie_name: str

    session_ttl_seconds: int
    login_token_ttl_seconds: int

    # Whether the Authorization: Bearer header is believed at all, and whether
    # the endpoints that mint keys exist. Off stops existing keys from
    # authenticating too -- the rows stay, so switching it back on restores
    # them.
    api_keys_enabled: bool
    # How long a new key lasts when the request does not say. Bounded so a
    # forgotten key eventually stops being a live credential.
    api_key_ttl_seconds: int

    # Encrypts session cookie payloads and, when encrypt_at_rest is on, every
    # record written to the database. Losing it means losing the data.
    secret_key: str = field(repr=False)
    encrypt_at_rest: bool = True

    rate_limit_max: int = 120
    rate_limit_window_seconds: int = 60
    body_limit_bytes: int = 1048576

    # proxy_header is believed only when trust_proxy is on *and* the
    # connection comes from an address in trusted_proxies (IPs or CIDR
    # ranges). The switch alone would let any client that reaches the server
    # directly name its own IP and walk past the rate limiter, so validate()
    # refuses one without the other.
    trust_proxy: bool = False
    trusted_proxies: tuple[str, ...] = ()
    proxy_header: str = "X-Forwarded-For"

    # Video processing. scan_workers is already resolved: 0 in the file means
    # "CPU cores minus one", and that is worked out here, once.
    jobs_max_concurrent: int = 1
    jobs_scan_workers: int = 1
    jobs_extract_workers: int = 6
    jobs_keep_source: bool = False

    upload_max_bytes: int = 21474836480
    upload_chunk_bytes: int = 33554432

    youtube_enabled: bool = True
    youtube_max_height: int = 1080
    youtube_max_playlist_items: int = 200

    # Empty client_id means the Google Slides export is switched off.
    google_client_id: str = ""
    google_client_secret: str = field(default="", repr=False)
    google_redirect_url: str = ""

    @property
    def google_enabled(self) -> bool:
        return self.google_client_id != ""

    @property
    def jobs_dir(self) -> Path:
        return self.storage_dir / "jobs"

    @property
    def uploads_dir(self) -> Path:
        return self.storage_dir / "uploads"

    @property
    def tmp_dir(self) -> Path:
        """Scratch space. Not /tmp: the container's root filesystem is
        read-only and its tmpfs is small, and video work is large."""
        return self.storage_dir / "tmp"

    @property
    def listen_address(self) -> str:
        return f"{self.host}:{self.port}"


# ---------------------------------------------------------------------------
# where things live
# ---------------------------------------------------------------------------


def default_app_dir() -> Path:
    """~/.config/<slug>. Everything lands in one directory rather than being
    split across XDG's config/data/state roots: this app's state is small, and
    one directory to back up is worth more here than strict adherence to a
    layout designed for larger, more separable applications."""
    try:
        home = Path.home()
    except (RuntimeError, KeyError):
        home = None
    if home is not None and str(home) not in ("", "/"):
        return home / ".config" / APP_SLUG
    # No usable home directory (some minimal environments). Fall back to the
    # working directory rather than failing to start.
    return Path(".") / f"{APP_SLUG}-data"


def locate() -> tuple[Path, bool]:
    """Find the config.yaml in use: the pinned path inside the container
    image, otherwise ./config.yaml, otherwise the one in the default app
    directory. When none exists, the path is where one would be created.

    ./config.yaml comes first because that is a source checkout, which is
    also where dockerRun.sh reads it. Run from anywhere else, the app falls
    through to ~/.config/<slug>/ and finds the same file every time."""
    if PINNED.config_file:
        path = Path(PINNED.config_file)
        return path, path.is_file()
    local = Path(CONFIG_FILE_NAME).absolute()
    if local.is_file():
        return local, True
    path = (default_app_dir() / CONFIG_FILE_NAME).absolute()
    return path, path.is_file()


def resolve_path(base: Path, value: str) -> str:
    """Make a config path absolute against the directory holding config.yaml,
    so the same file means the same thing whatever directory the process was
    started from."""
    if value == "" or os.path.isabs(value):
        return value
    return str(base / value)


# ---------------------------------------------------------------------------
# strict decoding
# ---------------------------------------------------------------------------

_INT_TAG = "tag:yaml.org,2002:int"
_BOOL_TAG = "tag:yaml.org,2002:bool"
_NULL_TAG = "tag:yaml.org,2002:null"
_constructor = yaml.constructor.SafeConstructor()


def _scalar(node: yaml.Node, wanted: type, key: str, label: str) -> Any:
    line = node.start_mark.line + 1
    where = f"{label}: line {line}: {key}"
    if not isinstance(node, yaml.ScalarNode):
        raise ConfigError(f"{where} must be a single value, not a list or mapping")
    if wanted is str:
        # Any scalar is accepted as text -- `cpus: 1` means "1" -- and an empty
        # value means an empty string, which is what `app_dir:` reads as.
        return "" if node.tag == _NULL_TAG else str(node.value)
    if node.tag == _NULL_TAG:
        raise ConfigError(f"{where} has no value (delete the line to use the default)")
    if wanted is bool:
        if node.tag != _BOOL_TAG:
            raise ConfigError(f"{where} must be true or false, got {node.value!r}")
        return _constructor.construct_yaml_bool(node)
    if wanted is int:
        if node.tag != _INT_TAG:
            raise ConfigError(f"{where} must be a whole number, got {node.value!r}")
        return _constructor.construct_yaml_int(node)
    raise ConfigError(f"{where} has an unsupported type")  # pragma: no cover


def _string_list(node: yaml.Node, key: str, label: str) -> list[str]:
    """A YAML list of plain values, each read as text."""
    if isinstance(node, yaml.ScalarNode) and node.tag == _NULL_TAG:
        return []
    if not isinstance(node, yaml.SequenceNode):
        raise ConfigError(f"{label}: line {node.start_mark.line + 1}: {key} must be a list, like [127.0.0.1]")
    return [_scalar(item, str, key, label) for item in node.value]


def _mapping_pairs(node: yaml.Node, path: str, label: str) -> list[tuple[str, int, yaml.Node]]:
    """The key/value pairs of a mapping node, refusing duplicate keys -- a
    YAML parser keeps the last one silently, which is another way for a
    setting to quietly not be the one you wrote."""
    line = node.start_mark.line + 1
    if not isinstance(node, yaml.MappingNode):
        raise ConfigError(f"{label}: line {line}: {path or 'the file'} must be a mapping of keys to values")
    seen: set[str] = set()
    pairs = []
    for key_node, value_node in node.value:
        key = str(key_node.value)
        dotted = f"{path}.{key}" if path else key
        if key in seen:
            raise ConfigError(f"{label}: line {key_node.start_mark.line + 1}: {dotted} appears twice")
        seen.add(key)
        pairs.append((key, key_node.start_mark.line + 1, value_node))
    return pairs


def decode_strict(raw: str, label: str) -> dict[str, dict[str, Any]]:
    """Decode a config.yaml into {section: {key: value}}, holding every key
    to the schema. An empty file is a valid file that sets nothing."""
    try:
        root = yaml.compose(raw, Loader=yaml.SafeLoader)
    except yaml.YAMLError as error:
        raise ConfigError(f"{label}: {error}") from None
    result: dict[str, dict[str, Any]] = {}
    if root is None or (isinstance(root, yaml.ScalarNode) and root.tag == _NULL_TAG):
        return result
    for section_name, section_line, section_node in _mapping_pairs(root, "", label):
        schema = SECTIONS.get(section_name)
        if schema is None:
            known = ", ".join(SECTIONS)
            raise ConfigError(f"{label}: line {section_line}: unknown section {section_name!r} (known: {known})")
        values: dict[str, Any] = {}
        result[section_name] = values
        if isinstance(section_node, yaml.ScalarNode) and section_node.tag == _NULL_TAG:
            continue
        types = {f.name: f.type for f in fields(schema)}
        for key, key_line, value_node in _mapping_pairs(section_node, section_name, label):
            dotted = f"{section_name}.{key}"
            if key not in types:
                raise ConfigError(f"{label}: line {key_line}: unknown key {dotted}")
            if types[key] == "list[str]":
                values[key] = _string_list(value_node, dotted, label)
                continue
            wanted = {"str": str, "int": int, "bool": bool}[types[key]]
            values[key] = _scalar(value_node, wanted, dotted, label)
    return result


def _build(values: dict[str, dict[str, Any]]) -> File:
    sections = {}
    for name, schema in SECTIONS.items():
        given = values.get(name, {})
        missing = [f.name for f in fields(schema) if f.name not in given]
        if missing:
            raise ConfigError(f"defaults.yaml is missing {', '.join(f'{name}.{key}' for key in missing)}")
        sections[name] = schema(**given)
    return File(**sections)


def defaults() -> File:
    """defaults.yaml on its own: the configuration a missing key falls back
    to."""
    try:
        return _build(decode_strict(DEFAULTS_PATH.read_text(encoding="utf-8"), "defaults.yaml"))
    except ConfigError as error:
        raise ConfigError(f"the shipped defaults.yaml is invalid: {error}") from None


def parse(raw: str, label: str) -> File:
    """Decode a config.yaml over the defaults and validate the result. It does
    not require a secret key; load() and `config check` do."""
    merged = decode_strict(DEFAULTS_PATH.read_text(encoding="utf-8"), "defaults.yaml")
    for section, values in decode_strict(raw, label).items():
        merged.setdefault(section, {}).update(values)
    file = _build(merged)
    problems = validate(file)
    if problems:
        raise ConfigError(f"{label}: " + "; ".join(problems))
    return file


def read_file(path: Path) -> File:
    """Read and validate one config.yaml without touching anything else on
    disk."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"could not read {path}: {error.strerror}") from None
    return parse(raw, str(path))


def current() -> tuple[File, Path, bool]:
    """The file the server would use, or the defaults when there is no file
    yet. It never writes anything, which is what the shell-facing commands
    and the healthcheck need."""
    path, exists = locate()
    if exists:
        return read_file(path), path, True
    return defaults(), path, False


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

_IMAGE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)*$")
_CONTAINER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_RESTART = re.compile(r"^(no|always|unless-stopped|on-failure(:[0-9]+)?)$")
_SIZE = re.compile(r"^[0-9]+[bkmgBKMG]?$")
_CPUS = re.compile(r"^[0-9]+(\.[0-9]+)?$")
_SECRET = re.compile(r"^[0-9a-fA-F]{64}$")


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _proxy_network(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(value.strip(), strict=False)
    except ValueError:
        return None


def trusted_proxy_networks(cfg: "Config") -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """server.trusted_proxies parsed, for the rate limiter. validate() has
    already refused anything that does not parse."""
    return [network for network in (_proxy_network(entry) for entry in cfg.trusted_proxies) if network is not None]


def validate(file: File) -> list[str]:
    """Reject values that would fail later in a confusing way -- a port out
    of range, a Docker size the daemon would refuse. The docker values also
    end up in shell commands, so they are held to strict patterns rather than
    trusted."""
    problems: list[str] = []

    def check(ok: bool, message: str) -> None:
        if not ok:
            problems.append(message)

    def path_ok(value: str) -> bool:
        return value.strip() != "" and not any(c in value for c in ":\n\r")

    check(file.app.name.strip() != "", "app.name must not be empty")

    server = file.server
    check(server.host.strip() != "", "server.host must not be empty")
    check(1 <= server.port <= 65535, f"server.port must be 1-65535, got {server.port}")
    check(server.cookie_name.strip() != "", "server.cookie_name must not be empty")
    check(server.proxy_header.strip() != "", "server.proxy_header must not be empty")
    check(
        not server.trust_proxy or len(server.trusted_proxies) > 0,
        "server.trust_proxy is true but server.trusted_proxies is empty -- "
        "list the address or CIDR range your reverse proxy connects from",
    )
    for entry in server.trusted_proxies:
        network = _proxy_network(entry)
        check(network is not None, f"server.trusted_proxies entry {entry!r} must be an IP address or a CIDR range like 10.0.0.0/8")
        check(
            network is None or network.prefixlen > 0,
            f"server.trusted_proxies entry {entry!r} trusts every address, which lets any client forge its own IP",
        )
    check(server.body_limit_bytes > 0, "server.body_limit_bytes must be positive")
    check(server.rate_limit_max > 0, "server.rate_limit_max must be positive")
    check(server.rate_limit_window_seconds > 0, "server.rate_limit_window_seconds must be positive")

    auth = file.auth
    check(auth.session_ttl_seconds > 0, "auth.session_ttl_seconds must be positive")
    check(auth.login_token_ttl_seconds > 0, "auth.login_token_ttl_seconds must be positive")
    check(auth.api_key_ttl_seconds > 0, "auth.api_key_ttl_seconds must be positive")

    jobs = file.jobs
    check(1 <= jobs.max_concurrent <= 16, "jobs.max_concurrent must be 1-16")
    check(0 <= jobs.scan_workers <= 64, "jobs.scan_workers must be 0-64 (0 = CPU cores minus one)")
    check(1 <= jobs.extract_workers <= 32, "jobs.extract_workers must be 1-32")

    uploads = file.uploads
    check(uploads.max_bytes > 0, "uploads.max_bytes must be positive")
    check(
        1048576 <= uploads.chunk_bytes <= 268435456,
        "uploads.chunk_bytes must be between 1 MiB (1048576) and 256 MiB (268435456)",
    )

    youtube = file.youtube
    check(144 <= youtube.max_height <= 4320, "youtube.max_height must be 144-4320")
    check(1 <= youtube.max_playlist_items <= 5000, "youtube.max_playlist_items must be 1-5000")

    google = file.google
    if google.client_id.strip():
        check(google.client_secret.strip() != "", "google.client_secret is required when google.client_id is set")
    if google.redirect_url.strip():
        check(
            google.redirect_url.startswith(("http://", "https://"))
            and google.redirect_url.rstrip("/").endswith("/api/google/callback"),
            "google.redirect_url must be empty or the full http(s) address of /api/google/callback",
        )

    docker = file.docker
    check(bool(_IMAGE.match(docker.image)), f"docker.image {docker.image!r} is not a valid lowercase image name")
    check(bool(_CONTAINER.match(docker.container)), f"docker.container {docker.container!r} is not a valid container name")
    check(_valid_ip(docker.bind_address), f"docker.bind_address {docker.bind_address!r} must be an IP address")
    check(1 <= docker.host_port <= 65535, f"docker.host_port must be 1-65535, got {docker.host_port}")
    check(path_ok(docker.data_dir), "docker.data_dir must be a path with no ':' in it")
    check(bool(_RESTART.match(docker.restart)), f"docker.restart must be no, always, unless-stopped or on-failure[:N], got {docker.restart!r}")
    check(bool(_SIZE.match(docker.memory)), f"docker.memory must look like 256m, got {docker.memory!r}")
    check(bool(_CPUS.match(docker.cpus)), f"docker.cpus must be a number like 1 or 0.5, got {docker.cpus!r}")
    check(docker.pids_limit > 0, "docker.pids_limit must be positive")
    check(bool(_SIZE.match(docker.tmpfs_size)), f"docker.tmpfs_size must look like 16m, got {docker.tmpfs_size!r}")

    return problems


def default_google_redirect(port: int) -> str:
    """Where Google sends people back when google.redirect_url is empty.
    localhost rather than 127.0.0.1: Google accepts plain http only for
    localhost redirect URIs."""
    return f"http://localhost:{port}/api/google/callback"


def check_secret_key(file: File, path: Path) -> None:
    """Separate from validate() because only a process that will actually use
    the key needs one.

    An empty key is refused rather than filled in. A file that already exists
    was created with a key, so an empty one means it was removed -- and
    quietly generating a replacement would make every existing record
    permanently unreadable."""
    key = file.server.secret_key.strip()
    if key == "":
        raise ConfigError(
            f"server.secret_key is empty in {path}\n"
            "  If this app already has data, put back the key it was written with -- a new key cannot read it.\n"
            "  For a fresh install, generate one with: openssl rand -hex 32"
        )
    if not _SECRET.match(key):
        raise ConfigError(f"server.secret_key in {path} must be 64 hex characters (32 bytes), got {len(key)} characters")


def permission_warning(path: Path) -> str:
    """Flag a config.yaml anyone on the machine can read. The file holds the
    secret key, so that is everyone's copy of it."""
    if os.name == "nt":
        return ""
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return ""
    if mode & 0o004:
        return f"{path} is readable by every user (mode {mode:04o}) and holds the secret key -- chmod 600 it"
    return ""


# ---------------------------------------------------------------------------
# creating a config.yaml
# ---------------------------------------------------------------------------

_SECRET_KEY_LINE = re.compile(r'^(\s*secret_key:\s*)""', re.MULTILINE)


def new_file_content() -> str:
    """defaults.yaml with a freshly generated secret key -- the whole file,
    comments included, so the result documents itself."""
    from app.core import encryption  # imported here so `health` stays light

    text = DEFAULTS_PATH.read_text(encoding="utf-8")
    matches = _SECRET_KEY_LINE.findall(text)
    if len(matches) != 1:
        raise ConfigError(f'defaults.yaml must contain exactly one `secret_key: ""` line, found {len(matches)}')
    key = encryption.generate_key_hex()
    return _SECRET_KEY_LINE.sub(lambda match: f'{match.group(1)}"{key}"', text)


def write_new(path: Path) -> None:
    """Create path from new_file_content(), refusing to overwrite: an existing
    config.yaml holds a key that existing data depends on."""
    content = new_file_content()
    try:
        # 0700 because the secret key lives in here.
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as error:
        raise ConfigError(f"could not create {path}: {error.strerror}") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


# ---------------------------------------------------------------------------


def load() -> Config:
    """Find config.yaml (creating it with a fresh key if there is none),
    validate it, and make sure the app directory exists. The server and the
    manage CLI both start here."""
    path, exists = locate()
    created = False
    if not exists:
        if in_container():
            raise ConfigError(f"no config at {path} -- dockerRun.sh mounts config.yaml there")
        write_new(path)
        created = True

    file = read_file(path)
    check_secret_key(file, path)

    base = path.parent

    app_dir = default_app_dir()
    if file.server.app_dir:
        app_dir = Path(resolve_path(base, file.server.app_dir))
    if PINNED.app_dir:
        app_dir = Path(PINNED.app_dir)
    app_dir = app_dir.absolute()

    storage_dir = app_dir / STORAGE_DIR_NAME
    for directory, purpose in (
        (app_dir, "app directory"),
        (storage_dir, "storage directory"),
        (storage_dir / "jobs", "jobs directory"),
        (storage_dir / "uploads", "uploads directory"),
        (storage_dir / "tmp", "scratch directory"),
    ):
        try:
            # 0700 because the database lives in here.
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            raise ConfigError(f"could not create {purpose} {str(directory)!r}: {error.strerror}") from None

    # An operator can drop a language.yaml into the app directory to reword
    # the UI without touching the code. Failing that, the one beside
    # config.yaml (a source checkout) is used, and failing that the copy that
    # ships with the code (app/core/assets.py decides).
    language_file = resolve_path(base, file.server.language_file)
    if language_file == "":
        candidate = app_dir / LANGUAGE_FILE_NAME
        language_file = str(candidate if candidate.is_file() else base / LANGUAGE_FILE_NAME)

    return Config(
        project_name=file.app.name,
        config_file=path,
        config_created=created,
        config_warning=permission_warning(path),
        app_dir=app_dir,
        database_path=app_dir / DATABASE_FILE_NAME,
        storage_dir=storage_dir,
        static_dir=resolve_path(base, file.server.static_dir),
        language_file=language_file,
        host=PINNED.host or file.server.host,
        port=file.server.port,
        secure_cookies=file.server.secure_cookies,
        cookie_name=file.server.cookie_name,
        session_ttl_seconds=file.auth.session_ttl_seconds,
        login_token_ttl_seconds=file.auth.login_token_ttl_seconds,
        api_keys_enabled=file.auth.api_keys,
        api_key_ttl_seconds=file.auth.api_key_ttl_seconds,
        secret_key=file.server.secret_key.strip(),
        encrypt_at_rest=file.server.encrypt_at_rest,
        rate_limit_max=file.server.rate_limit_max,
        rate_limit_window_seconds=file.server.rate_limit_window_seconds,
        body_limit_bytes=file.server.body_limit_bytes,
        trust_proxy=file.server.trust_proxy,
        trusted_proxies=tuple(entry.strip() for entry in file.server.trusted_proxies),
        proxy_header=file.server.proxy_header,
        jobs_max_concurrent=file.jobs.max_concurrent,
        jobs_scan_workers=file.jobs.scan_workers or max(1, (os.cpu_count() or 2) - 1),
        jobs_extract_workers=file.jobs.extract_workers,
        jobs_keep_source=file.jobs.keep_source,
        upload_max_bytes=file.uploads.max_bytes,
        upload_chunk_bytes=file.uploads.chunk_bytes,
        youtube_enabled=file.youtube.enabled,
        youtube_max_height=file.youtube.max_height,
        youtube_max_playlist_items=file.youtube.max_playlist_items,
        google_client_id=file.google.client_id.strip(),
        google_client_secret=file.google.client_secret.strip(),
        google_redirect_url=file.google.redirect_url.strip() or default_google_redirect(file.server.port),
    )
