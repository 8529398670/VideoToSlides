# syntax=docker/dockerfile:1

# --- build stage -----------------------------------------------------------
# Only this stage runs pip. Nothing it downloads or caches survives into the
# shipped image except the installed packages themselves.
FROM python:3.14-alpine AS builder

WORKDIR /build
COPY requirements.txt .

# --only-binary=:all: means every dependency must arrive as a prebuilt wheel.
# They all ship musllinux wheels, so nothing is compiled; if a future
# dependency does not, the build fails loudly here instead of quietly needing
# gcc and headers. --prefix collects the result in one directory the runtime
# stage can copy whole.
RUN pip install --no-cache-dir --only-binary=:all: --prefix=/install -r requirements.txt

# --- runtime stage ---------------------------------------------------------
FROM python:3.14-alpine AS runtime

# The uid/gid are fixed at 10001 rather than left to Alpine's system range.
# dockerRun.sh grants this user the host's data directory and this group read
# access to config.yaml; a number in the low hundreds is likely to belong to a
# real account on a Linux host, and that account would get the same access.
#
# pip is removed: nothing installs packages at runtime, and a package manager
# is one more tool for anyone who gets code execution in here.
# ffmpeg decodes and samples the videos; deno is the JavaScript runtime
# yt-dlp needs for YouTube's player code. Both from Alpine's own repositories,
# so they are signed packages rather than downloaded binaries.
RUN apk add --no-cache ffmpeg deno \
 && addgroup -S -g 10001 app \
 && adduser -S -u 10001 -G app -h /app -s /sbin/nologin app \
 && python -m pip uninstall --yes --quiet pip

WORKDIR /app

COPY --from=builder /install /usr/local
COPY app ./app
COPY static ./static
COPY language.yaml ./

# Version metadata, passed by scripts/docker.sh so that
# `docker exec <name> python -m app version` says which build is running.
ARG VERSION=dev
ARG COMMIT=unknown
ARG BUILD_DATE=unknown

# Three things, all only ever done here:
#
#   layout_pinned.py   the image's fixed layout -- config at /app/config.yaml,
#                      state in /app/data, listen on 0.0.0.0. This is what
#                      replaces ENV, and why `docker exec <name> python -m app
#                      manage ...` needs nothing extra (app/core/layout.py).
#   build_info.py      VERSION/COMMIT/BUILD_DATE for the version command. The
#                      values arrive as arguments, so no quoting can break the
#                      generated file.
#   compileall         bytecode written now, because --read-only means the
#                      process can never write .pyc files later.
RUN cp app/core/layout_container.py app/core/layout_pinned.py \
 && python -c 'import sys; open("app/build_info.py", "w").write("VERSION = %r\nCOMMIT = %r\nBUILD_DATE = %r\n" % tuple(sys.argv[1:4]))' \
      "$VERSION" "$COMMIT" "$BUILD_DATE" \
 && python -m compileall -q app

# There is no ENV here and no config.yaml in the image (.dockerignore keeps it
# out -- it holds the secret key, and an image layer is forever). dockerRun.sh
# mounts the project's config.yaml at /app/config.yaml at run time, so one
# image serves any deployment and a config change needs a restart, not a
# rebuild.
#
# To run the shipped UI with different wording, put a language.yaml in the
# data directory (/app/data/language.yaml); it wins over the bundled one.

# Only the data directory is writable, and only by the app user. Everything
# else stays owned by root and read-only to the process -- so even a bug that
# lets someone run code in here cannot rewrite the app or the frontend.
# dockerRun.sh mounts /app/data as a volume and runs the container with
# --read-only, which is what makes that guarantee real rather than a
# convention.
RUN mkdir -p /app/data && chown app:app /app/data

# Note: this ownership survives into a *named* volume, but a bind mount
# shadows it with the host's ownership. dockerRun.sh uses a bind mount, so the
# database stays visible on the host for backups, and chowns that directory to
# this user before starting the container.

USER app

# No EXPOSE: the port is server.port in config.yaml, and dockerRun.sh
# publishes it from there. A hardcoded EXPOSE would be stale documentation the
# first time someone changed it.
#
# `python -m app health` reads the same config.yaml for the port and asks
# /api/health, which touches nothing -- so a slow database query can never get
# a healthy container killed.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-m", "app", "health"]

# -u: unbuffered, so the first-run login link reaches `docker logs` the
# moment it is printed rather than whenever a buffer fills.
ENTRYPOINT ["python", "-u", "-m", "app"]
