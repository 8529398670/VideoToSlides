#!/usr/bin/env bash
# Build the container image.
#
# Split from dockerRun.sh so that building and running are separately
# runnable: CI builds without starting anything, and a redeploy that only
# needs a restart does not have to rebuild. dockerRun.sh builds through the
# same function (scripts/docker.sh), so the common case is still one command.
#
# The image name is docker.image in config.yaml; with no config.yaml yet the
# default from app/core/defaults.yaml is used.
#
# Usage:
#   ./dockerBuild.sh              build <docker.image>:latest
#   ./dockerBuild.sh v1.2.3       build and additionally tag v1.2.3
set -euo pipefail
cd "$( dirname "${BASH_SOURCE[0]}" )"

source scripts/docker.sh
build_image "$@"
