#!/usr/bin/env bash
# One-time infrastructure: service accounts, IAM, Artifact Registry, Cloud Build
# staging bucket, MIG health check and firewall rule. Idempotent; run as a
# project owner. Day-to-day rollouts (03-05 / release.sh) need far fewer rights.
source "$(dirname "$0")/_common.sh"

create_sa() {
  local name="$1" display="$2"
  gcloud iam service-accounts describe "${name}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "${name}" --display-name "${display}"
}
create_sa mail-forwarder "mail-transporter Cloud Run forwarder"
create_sa mail-watcher   "mail-transporter GCE watcher"

# Watcher: write logs/metrics from the VM and pull the container image.
for role in roles/logging.logWriter roles/monitoring.metricWriter roles/artifactregistry.reader; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member "serviceAccount:${WATCHER_SA}" --role "${role}" --condition=None >/dev/null
done

# Cloud Build staging bucket (gcloud builds submit uploads the source here).
if ! gcloud storage buckets describe "gs://${BUILD_BUCKET}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUILD_BUCKET}" --location "${REGION}" --uniform-bucket-level-access
fi

# Health check + firewall rule for the watcher MIG's autohealing.
if ! gcloud compute health-checks describe "${HEALTH_CHECK}" >/dev/null 2>&1; then
  gcloud compute health-checks create http "${HEALTH_CHECK}" \
    --port "${HEALTH_PORT}" --request-path /healthz \
    --check-interval 60s --timeout 10s --healthy-threshold 1 --unhealthy-threshold 3
fi
if ! gcloud compute firewall-rules describe "${VM_NAME}-allow-health-check" >/dev/null 2>&1; then
  gcloud compute firewall-rules create "${VM_NAME}-allow-health-check" \
    --network default --direction INGRESS --action ALLOW --rules "tcp:${HEALTH_PORT}" \
    --source-ranges 35.191.0.0/16,130.211.0.0/22 --target-tags "${VM_NAME}"
fi

# Artifact Registry repository + cleanup policy (keep the image count tiny: 0.5 GB free).
if ! gcloud artifacts repositories describe "${AR_REPO}" --location "${REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${AR_REPO}" --repository-format docker --location "${REGION}"
fi
gcloud artifacts repositories set-cleanup-policies "${AR_REPO}" --location "${REGION}" \
  --policy "$(dirname "$0")/cleanup-policy.json" --no-dry-run >/dev/null
