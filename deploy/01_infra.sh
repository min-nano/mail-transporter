#!/usr/bin/env bash
# Service accounts, IAM, Artifact Registry. Idempotent.
source "$(dirname "$0")/_common.sh"

create_sa() {
  local name="$1" display="$2"
  gcloud iam service-accounts describe "${name}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "${name}" --display-name "${display}"
}
create_sa mail-forwarder "mail-transporter Cloud Run forwarder"
create_sa mail-watcher   "mail-transporter GCE watcher"
create_sa mail-scheduler "mail-transporter Cloud Scheduler"

# Watcher: write logs/metrics from the VM and pull the container image.
for role in roles/logging.logWriter roles/monitoring.metricWriter roles/artifactregistry.reader; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member "serviceAccount:${WATCHER_SA}" --role "${role}" --condition=None >/dev/null
done

# Artifact Registry repository + cleanup policy (keep the image count tiny: 0.5 GB free).
if ! gcloud artifacts repositories describe "${AR_REPO}" --location "${REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${AR_REPO}" --repository-format docker --location "${REGION}"
fi
gcloud artifacts repositories set-cleanup-policies "${AR_REPO}" --location "${REGION}" \
  --policy "$(dirname "$0")/cleanup-policy.json" --no-dry-run >/dev/null
