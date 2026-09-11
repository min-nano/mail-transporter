# shellcheck shell=bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "${HERE}/env.sh" ]]; then
  echo "deploy/env.sh not found. Copy deploy/env.example.sh to deploy/env.sh and edit it." >&2
  exit 1
fi
# shellcheck source=env.example.sh
source "${HERE}/env.sh"
gcloud config set project "${PROJECT_ID}" >/dev/null

service_url() {
  gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --format 'value(status.url)'
}
