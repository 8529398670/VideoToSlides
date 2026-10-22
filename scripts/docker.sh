# Shared by dockerBuild.sh and dockerRun.sh. Sourced, not run.
#
# build_image builds the image and then reads config.yaml *through it*: the
# app inside is the YAML parser, so these scripts get exactly the defaults and
# validation the server uses, and the host needs nothing but Docker -- not
# even Python. That ordering is also why the image name -- which lives in
# config.yaml like everything else -- is applied as a tag after the build
# rather than passed to it.
#
# Afterwards these are set: IMAGE_ID, CONFIG_FILE, VERSION, and every
# docker_* / server_* value printed by `config shell docker` (see
# app/core/config_command.py for the list).

CONFIG_FILE="$( pwd )/config.yaml"

# config_run runs the freshly built image in a throwaway container that can do
# nothing but read config.yaml: no network, read-only, no capabilities. It runs
# as the invoking user, so a 0600 config.yaml owned by that user is readable on
# Linux as well as macOS.
#
# The file is only mounted when it exists. Mounting a missing file makes Docker
# create an empty *directory* in its place, which then breaks every later run.
config_run() {
  local mount=()
  if [ -f "$CONFIG_FILE" ]; then
    mount=( -v "${CONFIG_FILE}:/app/config.yaml:ro" )
  fi
  docker run --rm --network none --read-only --cap-drop ALL \
    --user "$( id -u ):$( id -g )" \
    ${mount[@]+"${mount[@]}"} \
    "$IMAGE_ID" config "$@"
}

# build_image [extra-tag]
build_image() {
  local extra_tag="${1:-}"
  local commit build_date id_file assignments

  if [ -d "$CONFIG_FILE" ]; then
    echo "ERROR: ${CONFIG_FILE} is a directory. Docker creates one when asked to" >&2
    echo "       mount a file that does not exist. Remove it and re-run." >&2
    exit 1
  fi

  # Version metadata, stamped into the image so that
  # `docker exec <name> python -m app version` identifies the build.
  VERSION="$( git describe --tags --always --dirty 2>/dev/null || echo "dev" )"
  commit="$( git rev-parse --short HEAD 2>/dev/null || echo "unknown" )"
  build_date="$( date -u +%Y-%m-%dT%H:%M:%SZ )"

  id_file="$( mktemp )"
  echo "Building ${VERSION}"
  if ! docker build \
         --iidfile "$id_file" \
         --build-arg "VERSION=${VERSION}" \
         --build-arg "COMMIT=${commit}" \
         --build-arg "BUILD_DATE=${build_date}" \
         .; then
    rm -f "$id_file"
    exit 1
  fi
  IMAGE_ID="$( cat "$id_file" )"
  rm -f "$id_file"

  # With no config.yaml yet this reads the defaults, which is all a CI build
  # needs to name the image. dockerRun.sh creates the real file before it
  # starts anything.
  #
  # Assigned before it is eval'd so that a validation error stops the script:
  # `eval "$( failing )"` would quietly eval nothing and carry on.
  assignments="$( config_run shell docker )"
  eval "$assignments"

  docker tag "$IMAGE_ID" "${docker_image}:latest"
  if [ -n "$extra_tag" ]; then
    docker tag "$IMAGE_ID" "${docker_image}:${extra_tag}"
    echo "Also tagged ${docker_image}:${extra_tag}"
  fi
  echo "Built ${docker_image}:latest (${VERSION})"
}

# create_config writes config.yaml from the image's defaults with a fresh
# secret key. It goes through a temporary file so that a failure part-way
# leaves no half-written config.yaml behind to be mistaken for a real one.
create_config() {
  ( umask 077; docker run --rm --network none "$IMAGE_ID" config init --file - > "${CONFIG_FILE}.new" )
  mv "${CONFIG_FILE}.new" "$CONFIG_FILE"
}
