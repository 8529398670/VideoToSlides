#!/usr/bin/env bash
# Run VideoToSlides from this checkout, in a local virtualenv -- no Docker.
#
#   ./run.sh                          start the web server
#   ./run.sh manage list-users        any `python -m app ...` command, in the venv
#   ./run.sh manage reissue-login --user-id 1
#
# Every run makes sure of four things first, and only does work when needed:
#
#   1. .venv exists, built with Python 3.11 or newer.
#   2. The packages in requirements.txt are installed. A hash of the file is
#      kept in .venv, so edit requirements.txt and the next run reinstalls;
#      leave it alone and nothing is downloaded.
#   3. ffmpeg and ffprobe are on PATH (the slide pipeline is built on them),
#      and deno or node is, for YouTube downloads -- a warning, not an error,
#      because uploads work without one.
#   4. ./config.yaml exists. A fresh checkout has none (it is gitignored: it
#      holds the secret key), so one is written with a new key. Since this
#      script is the plain-http local run, that new file gets
#      server.secure_cookies: false -- a Secure cookie is silently dropped over
#      http://, and logging in would appear to work and then do nothing. Put it
#      back to true if you serve this over https.
#
# Like the rest of the app, this reads no environment variables: settings live
# in config.yaml. The first start on an empty database prints a one-time
# login link -- open it to sign in as the admin.
set -euo pipefail
cd "$( dirname "${BASH_SOURCE[0]}" )"

VENV=".venv"
VENV_PYTHON="${VENV}/bin/python"
STAMP="${VENV}/.requirements.sha256"
MIN_MINOR=11

# -- 1. a Python new enough, and the venv -------------------------------------

python_ok() {
  "$1" -c "import sys; sys.exit(0 if sys.version_info >= (3, ${MIN_MINOR}) else 1)" >/dev/null 2>&1
}

if [ ! -x "$VENV_PYTHON" ] || ! python_ok "$VENV_PYTHON"; then
  PYTHON=""
  for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
      PYTHON="$candidate"
      break
    fi
  done
  if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.${MIN_MINOR} or newer is required (python3 --version to check)." >&2
    exit 1
  fi
  echo "[run] creating ${VENV} with $( "$PYTHON" --version )"
  rm -rf "$VENV"
  "$PYTHON" -m venv "$VENV"
fi

# -- 2. dependencies, only when requirements.txt changed ----------------------

WANT="$( "$VENV_PYTHON" -c 'import hashlib; print(hashlib.sha256(open("requirements.txt", "rb").read()).hexdigest())' )"
HAVE="$( cat "$STAMP" 2>/dev/null || true )"
if [ "$WANT" != "$HAVE" ]; then
  echo "[run] installing requirements.txt into ${VENV}"
  "$VENV_PYTHON" -m pip install --quiet --disable-pip-version-check -r requirements.txt
  echo "$WANT" > "$STAMP"
fi

# -- 3. the programs the pipeline runs ----------------------------------------

missing=()
for tool in ffmpeg ffprobe; do
  command -v "$tool" >/dev/null 2>&1 || missing+=( "$tool" )
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "ERROR: ${missing[*]} not found on PATH. Slide extraction needs ffmpeg:" >&2
  echo "         macOS:          brew install ffmpeg" >&2
  echo "         Debian/Ubuntu:  sudo apt install ffmpeg" >&2
  exit 1
fi
if ! command -v deno >/dev/null 2>&1 && ! command -v node >/dev/null 2>&1; then
  echo "[run] warning: neither deno nor node is on PATH -- YouTube downloads may fail" >&2
  echo "      (yt-dlp needs a JavaScript runtime; e.g. brew install deno). Uploads are unaffected." >&2
fi

# -- 4. config.yaml -----------------------------------------------------------

if [ ! -f config.yaml ]; then
  "$VENV_PYTHON" -m app config init >/dev/null
  "$VENV_PYTHON" - <<'EOF'
from pathlib import Path
path = Path("config.yaml")
text = path.read_text(encoding="utf-8")
path.write_text(text.replace(
    "  secure_cookies: true\n",
    "  secure_cookies: false            # set by run.sh for plain http://localhost; true behind https\n",
    1,
), encoding="utf-8")
EOF
  echo "[run] wrote config.yaml (mode 0600, new secret key, secure_cookies: false for local http)."
  echo "      Back it up: the secret key is the only thing that can read the database."
fi

# -- go -----------------------------------------------------------------------

# exec, so Ctrl+C and signals reach the server directly; -u so the first-run
# login link is printed the moment it exists.
exec "$VENV_PYTHON" -u -m app "$@"
