#!/usr/bin/env bash
# Build (via scripts/docker.sh) and run the container.
#
# There is deliberately no docker-compose here. This app is one container, so
# compose would add a second configuration format that hides the same flags
# behind YAML keys you have to look up. A single `docker run` you can read top
# to bottom is easier to audit, and every hardening choice below is visible on
# the line where it takes effect.
#
# Every value below comes from config.yaml: the docker: section for how the
# container runs, and server.port for the container side of the published
# port. Nothing is read from the environment and nothing is passed in with -e
# -- the container reads the same config.yaml, mounted read-only.
set -euo pipefail
cd "$( dirname "${BASH_SOURCE[0]}" )"

source scripts/docker.sh
build_image

# First run on this checkout: config.yaml is gitignored, so a fresh clone has
# none. Write one and stop rather than starting on defaults nobody has looked
# at -- secure_cookies and trust_proxy both depend on what sits in front of
# this, and only the operator knows that.
if [ ! -f "$CONFIG_FILE" ]; then
  create_config
  cat <<NOTE

Wrote ${CONFIG_FILE}
(mode 0600, with a new secret key). That one file configures the server and
the container. Review it before the first start, in particular:

  server.secure_cookies   true behind HTTPS; false only for plain http://localhost
  server.trust_proxy      true once a reverse proxy you control is in front,
  server.trusted_proxies  with the address it connects from (see config.yaml)
  docker.bind_address     127.0.0.1 keeps it reachable only from this host
  docker.host_port        the port published there

Then run ./dockerRun.sh again. Back config.yaml up: its secret_key is the only
thing that can read the database.

NOTE
  exit 0
fi

# docker.data_dir is relative to the project directory, like every docker:
# path. It is mounted at /app/data and holds the database and app storage. config.yaml is mounted separately and stays outside it, so
# a leaked backup of the data directory alone cannot be decrypted.
case "$docker_data_dir" in
  /*) DATA_DIR="$docker_data_dir" ;;
  *)  DATA_DIR="$( pwd )/${docker_data_dir#./}" ;;
esac
mkdir -p "$DATA_DIR"

# The container's unprivileged user. Its uid is read back from the image rather
# than hardcoded here, so this keeps working if the Dockerfile ever changes it.
APP_UID="$( docker run --rm --entrypoint sh "$IMAGE_ID" -c 'id -u app' )"
APP_GID="$( docker run --rm --entrypoint sh "$IMAGE_ID" -c 'id -g app' )"

# Make sure Docker can see this project at all.
#
# Docker Desktop, colima, and friends run the daemon in a VM that shares only
# some host paths with it (colima: your home directory). Mount a path outside
# those and Docker quietly mounts an empty directory from inside the VM
# instead: config.yaml shows up as a directory, and the database would land in
# the VM where no backup of this host will ever find it. Checking that the
# file arrives as a file catches that before anything starts.
if ! docker run --rm -v "${CONFIG_FILE}:/config.yaml:ro" \
     --entrypoint sh "$IMAGE_ID" -c 'test -f /config.yaml' >/dev/null 2>&1; then
  echo "" >&2
  echo "ERROR: Docker cannot see ${CONFIG_FILE}" >&2
  echo "" >&2
  echo "       The project is probably outside the paths your Docker VM shares" >&2
  echo "       with this host (colima shares your home directory; Docker Desktop:" >&2
  echo "       Settings > Resources > File sharing). Move the project under a" >&2
  echo "       shared path, or share this one, and re-run." >&2
  echo "" >&2
  exit 1
fi

# Make sure that user can read config.yaml.
#
# The file is 0600 and owned by you, which the container user cannot read on
# Linux. Granting the image's group read access (0640) rather than handing the
# file over keeps you its owner, so you can go on editing it. The image's
# uid/gid are fixed at 10001 so that group is very unlikely to belong to a real
# account on the host.
#
# Best effort, because the right answer differs by platform: Docker Desktop's
# file sharing usually ignores guest ownership, so every uid can already read
# it, while colima's sshfs mounts enforce the host's modes and root in the
# container cannot change them -- there, the fix is the sudo line printed
# below. So the result is tested rather than trusted.
docker run --rm --user 0:0 -v "${CONFIG_FILE}:/config.yaml" \
  --entrypoint sh "$IMAGE_ID" -c "chgrp ${APP_GID} /config.yaml && chmod 640 /config.yaml" >/dev/null 2>&1 || true

if ! docker run --rm --user "${APP_UID}:${APP_GID}" -v "${CONFIG_FILE}:/config.yaml:ro" \
     --entrypoint sh "$IMAGE_ID" -c 'test -r /config.yaml' >/dev/null 2>&1; then
  echo "" >&2
  echo "ERROR: the container runs as uid ${APP_UID}, which cannot read" >&2
  echo "       ${CONFIG_FILE}" >&2
  echo "" >&2
  echo "       Grant its group read access on the host and re-run:" >&2
  echo "         sudo chgrp ${APP_GID} \"${CONFIG_FILE}\" && chmod 640 \"${CONFIG_FILE}\"" >&2
  echo "" >&2
  exit 1
fi

# Make sure the same user can write the data directory.
#
# A bind mount ignores the image's own ownership: /app/data is chowned to "app"
# at build time, but mounting a host directory over it replaces that with
# whatever the host says. Get this wrong and the container starts, fails to
# create app.db, and crash-loops on "permission denied" -- a slow thing to
# diagnose from the outside, so it is worth settling here.
docker run --rm --user 0:0 -v "${DATA_DIR}:/data" \
  --entrypoint sh "$IMAGE_ID" -c "chown -R ${APP_UID}:${APP_GID} /data" >/dev/null 2>&1 || true

if ! docker run --rm --user "${APP_UID}:${APP_GID}" -v "${DATA_DIR}:/data" \
     --entrypoint sh "$IMAGE_ID" -c 'touch /data/.probe' >/dev/null 2>&1; then
  echo "" >&2
  echo "ERROR: the container runs as uid ${APP_UID}, which cannot write to" >&2
  echo "       ${DATA_DIR}" >&2
  echo "" >&2
  echo "       Without this the container will crash-loop on 'permission denied'." >&2
  echo "       Grant it on the host and re-run:" >&2
  echo "         sudo chown -R ${APP_UID}:${APP_GID} \"${DATA_DIR}\"" >&2
  echo "" >&2
  exit 1
fi

# The probe the container just wrote must be visible here. If it is not, the
# data directory is outside the paths the Docker VM shares (see above), and
# the database would be written somewhere this host cannot back up.
DATA_SHARED=false
[ -f "${DATA_DIR}/.probe" ] && DATA_SHARED=true
docker run --rm --user "${APP_UID}:${APP_GID}" -v "${DATA_DIR}:/data" \
  --entrypoint sh "$IMAGE_ID" -c 'rm -f /data/.probe' >/dev/null 2>&1 || true
if [ "$DATA_SHARED" != "true" ]; then
  echo "" >&2
  echo "ERROR: Docker cannot see ${DATA_DIR}" >&2
  echo "" >&2
  echo "       A file written there from inside the container did not appear on" >&2
  echo "       this host, so the directory is outside the paths your Docker VM" >&2
  echo "       shares. Point docker.data_dir under a shared path and re-run." >&2
  echo "" >&2
  exit 1
fi

if docker ps -a --format '{{.Names}}' | grep -qx "$docker_container"; then
  echo "Replacing existing container $docker_container..."
  docker rm -f "$docker_container" >/dev/null
fi

echo "Starting $docker_container on ${docker_bind_address}:${docker_host_port}..."
docker run -d \
  --name "$docker_container" \
  --restart "$docker_restart" \
  -p "$docker_publish" \
  -v "${DATA_DIR}:/app/data" \
  -v "${CONFIG_FILE}:/app/config.yaml:ro" \
  --read-only \
  --tmpfs "/tmp:rw,noexec,nosuid,size=${docker_tmpfs_size}" \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --pids-limit "$docker_pids_limit" \
  --memory "$docker_memory" \
  --cpus "$docker_cpus" \
  "${docker_image}:latest"

cat <<NOTE

Running. Two things on the host make up this deployment:

  ${CONFIG_FILE}
      every setting, and the secret key (mounted read-only at /app/config.yaml)
  ${DATA_DIR}/
      app.db and storage/ (mounted at /app/data)

Back them up together -- either one alone is useless.

What the flags above are doing:

  -p ${docker_publish}
                     docker.bind_address:docker.host_port:server.port. This
                     app does NOT terminate TLS -- put nginx/Caddy/Traefik in
                     front and let it hold the certificate.
  --read-only        The container cannot write to its own filesystem. Only
                     the data directory and the tmpfs are writable.
  --cap-drop ALL     No Linux capabilities at all.
  --no-new-privileges  A process in here can never gain more rights than it
                     started with, even via a setuid binary.
  (non-root)         Runs as the image's unprivileged "app" user.
  --pids-limit/--memory/--cpus  Blast radius limits (docker.pids_limit,
                     docker.memory, docker.cpus). Raise them deliberately for
                     real load rather than finding out under traffic.

NOTE

if [ "$server_secure_cookies" = "false" ]; then
  echo "Note: server.secure_cookies is false. Right for plain http://localhost; wrong"
  echo "      behind an HTTPS proxy, where it should be true."
  echo ""
fi
if [ "$server_trust_proxy" = "false" ]; then
  echo "Note: server.trust_proxy is false. Once a reverse proxy is in front, set it"
  echo "      true and list the proxy's address in server.trusted_proxies, or the"
  echo "      rate limiter sees every client as the proxy."
  echo ""
fi

cat <<NOTE
On a fresh database the server creates the first admin and prints a one-time
login link. Read it with:

  docker logs $docker_container

Then open https://your-host/login/<the-link> to sign in. Day to day, mint
further logins from the admin panel in the running app -- that goes through
the server and needs no downtime.

The CLI is for when you cannot get in at all. It works against the running
container -- SQLite lets it open the database alongside the live server:

  docker exec -it $docker_container python -m app manage reissue-login --user-id 1

If the container is stopped, run a one-off with the same two mounts:

  docker run --rm -v "${DATA_DIR}:/app/data" \\
    -v "${CONFIG_FILE}:/app/config.yaml:ro" \\
    ${docker_image}:latest manage reissue-login --user-id 1

NOTE
