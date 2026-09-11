#!/usr/bin/env bash
# Service accounts, IAM, Firestore database, Artifact Registry. Idempotent.
source "$(dirname "$0")/_common.sh"

create_sa() {
  local name="$1" display="$2"
  gcloud iam service-accounts describe "${name}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "${name}" --display-name "${display}"
}
create_sa mail-forwarder "mail-transporter Cloud Run forwarder"
create_sa mail-watcher   "mail-transporter GCE watcher"
create_sa mail-scheduler "mail-transporter Cloud Scheduler"

# Forwarder: Firestore read/write.
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member "serviceAccount:${RUN_SA}" --role roles/datastore.user --condition=None >/dev/null
# Watcher: write logs/metrics from the VM and pull the container image.
for role in roles/logging.logWriter roles/monitoring.metricWriter roles/artifactregistry.reader; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member "serviceAccount:${WATCHER_SA}" --role "${role}" --condition=None >/dev/null
done

# Firestore (Native mode). Only the "(default)" database is covered by the free tier.
if ! gcloud firestore databases describe --database='(default)' >/dev/null 2>&1; then
  gcloud firestore databases create --location "${FIRESTORE_LOCATION}" --type firestore-native
fi
# Auto-delete tracking documents once they expire (RETENTION_DAYS, default 30).
gcloud firestore fields ttls update expire_at \
  --collection-group forwarded_messages --enable-ttl --async >/dev/null 2>&1 || true

# Artifact Registry repository + cleanup policy (keep the image count tiny: 0.5 GB free).
if ! gcloud artifacts repositories describe "${AR_REPO}" --location "${REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${AR_REPO}" --repository-format docker --location "${REGION}"
fi
gcloud artifacts repositories set-cleanup-policies "${AR_REPO}" --location "${REGION}" \
  --policy "$(dirname "$0")/cleanup-policy.json" --no-dry-run >/dev/null
