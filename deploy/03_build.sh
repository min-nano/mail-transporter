#!/usr/bin/env bash
# Build the container image with Cloud Build and push it to Artifact Registry.
source "$(dirname "$0")/_common.sh"

TAG="${1:-$(git -C "${HERE}/.." rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
gcloud builds submit "${HERE}/.." \
  --config "${HERE}/../cloudbuild.yaml" \
  --substitutions "_IMAGE=${IMAGE},_TAG=${TAG}" \
  --gcs-source-staging-dir "gs://${BUILD_BUCKET}/source" \
  --quiet
echo "Built ${IMAGE}:${TAG} (also tagged latest)"
