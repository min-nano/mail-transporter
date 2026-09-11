#!/usr/bin/env bash
# Create (or update the container of) the free-tier e2-micro watcher VM.
source "$(dirname "$0")/_common.sh"
TAG="${1:-latest}"
URL="$(service_url)"

CONTAINER_ENV="ICLOUD_USER=${ICLOUD_USER},ICLOUD_PASSWORD_SECRET=projects/${PROJECT_ID}/secrets/${SECRET_ICLOUD},FORWARDER_URL=${URL},RETRIGGER_INTERVAL=600,IDLE_TIMEOUT=240"

if gcloud compute instances describe "${VM_NAME}" --zone "${ZONE}" >/dev/null 2>&1; then
  gcloud compute instances update-container "${VM_NAME}" --zone "${ZONE}" \
    --container-image "${IMAGE}:${TAG}" \
    --container-command python \
    --container-arg=-m --container-arg=mailtransporter.watcher \
    --container-env "${CONTAINER_ENV}" \
    --container-restart-policy always
else
  gcloud compute instances create-with-container "${VM_NAME}" --zone "${ZONE}" \
    --machine-type e2-micro \
    --boot-disk-size 10GB --boot-disk-type pd-standard \
    --service-account "${WATCHER_SA}" --scopes cloud-platform \
    --container-image "${IMAGE}:${TAG}" \
    --container-command python \
    --container-arg=-m --container-arg=mailtransporter.watcher \
    --container-env "${CONTAINER_ENV}" \
    --container-restart-policy always \
    --metadata google-logging-enabled=true,google-monitoring-enabled=true \
    --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring
fi
echo "Watcher VM ${VM_NAME} is running ${IMAGE}:${TAG}"
