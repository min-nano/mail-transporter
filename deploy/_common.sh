# shellcheck shell=bash
# Shared prelude for the deploy scripts.
#
# Configuration comes from deploy/env.sh (local use) or from the environment
# (CI). Only PROJECT_ID and ICLOUD_USER are required; everything else has a
# default below and can be overridden either way.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${HERE}/env.sh" ]]; then
  # shellcheck source=env.example.sh
  source "${HERE}/env.sh"
fi
for required in PROJECT_ID ICLOUD_USER; do
  if [[ -z "${!required:-}" ]]; then
    echo "${required} is not set. Copy deploy/env.example.sh to deploy/env.sh, or export it." >&2
    exit 1
  fi
done

export REGION="${REGION:-us-central1}"
export ZONE="${ZONE:-us-central1-a}"
export GMAIL_LABEL="${GMAIL_LABEL:-iCloud}"   # "none" disables the label
export SERVICE_NAME="${SERVICE_NAME:-mail-forwarder}"
export VM_NAME="${VM_NAME:-mail-watcher}"
export AR_REPO="${AR_REPO:-mail-transporter}"
export IMAGE="${IMAGE:-${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/mail-transporter}"
export RUN_SA="${RUN_SA:-mail-forwarder@${PROJECT_ID}.iam.gserviceaccount.com}"
export WATCHER_SA="${WATCHER_SA:-mail-watcher@${PROJECT_ID}.iam.gserviceaccount.com}"
export DEPLOYER_SA="${DEPLOYER_SA:-mail-deployer@${PROJECT_ID}.iam.gserviceaccount.com}"
export SECRET_ICLOUD="${SECRET_ICLOUD:-icloud-password}"
export SECRET_GMAIL="${SECRET_GMAIL:-gmail-oauth}"
export HEALTH_CHECK="${HEALTH_CHECK:-mail-watcher-health}"
export HEALTH_PORT="${HEALTH_PORT:-8080}"
export HEALTH_INITIAL_DELAY="${HEALTH_INITIAL_DELAY:-300}"
export BUILD_BUCKET="${BUILD_BUCKET:-${PROJECT_ID}_cloudbuild}"

gcloud config set project "${PROJECT_ID}" >/dev/null

service_url() {
  gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --format 'value(status.url)'
}
