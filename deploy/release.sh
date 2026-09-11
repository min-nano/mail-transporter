#!/usr/bin/env bash
# Build + roll out a new image to both Cloud Run and the watcher VM.
source "$(dirname "$0")/_common.sh"
TAG="${1:-$(git -C "${HERE}/.." rev-parse --short HEAD)}"
"${HERE}/03_build.sh" "${TAG}"
"${HERE}/04_deploy_forwarder.sh" "${TAG}"
"${HERE}/05_deploy_watcher.sh" "${TAG}"
