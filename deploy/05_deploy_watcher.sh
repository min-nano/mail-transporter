#!/usr/bin/env bash
# Create (or roll out a new image to) the watcher: a size-1 managed instance
# group of one free-tier e2-micro with autohealing on the watcher's /healthz.
source "$(dirname "$0")/_common.sh"
TAG="${1:-latest}"
URL="$(service_url)"

TEMPLATE="${VM_NAME}-$(echo "${TAG}" | tr -c 'a-z0-9\n' '-' | cut -c1-20)-$(date +%Y%m%d%H%M%S)"
CONTAINER_ENV="ICLOUD_USER=${ICLOUD_USER},ICLOUD_PASSWORD_SECRET=projects/${PROJECT_ID}/secrets/${SECRET_ICLOUD},FORWARDER_URL=${URL},RETRIGGER_INTERVAL=600,IDLE_TIMEOUT=240,HEALTH_PORT=${HEALTH_PORT}"

# The health check and firewall rule are created once by 01_infra.sh.

# 1. Instance template for this image tag (templates are immutable).
gcloud compute instance-templates create-with-container "${TEMPLATE}" \
  --machine-type e2-micro \
  --boot-disk-size 10GB --boot-disk-type pd-standard \
  --service-account "${WATCHER_SA}" --scopes cloud-platform \
  --tags "${VM_NAME}" \
  --container-image "${IMAGE}:${TAG}" \
  --container-command python \
  --container-arg=-m --container-arg=mailtransporter.watcher \
  --container-env "${CONTAINER_ENV}" \
  --container-restart-policy always \
  --metadata google-logging-enabled=true,google-monitoring-enabled=true \
  --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring

# 2. Managed instance group of size 1 with autohealing.
if gcloud compute instance-groups managed describe "${VM_NAME}" --zone "${ZONE}" >/dev/null 2>&1; then
  gcloud compute instance-groups managed rolling-action start-update "${VM_NAME}" --zone "${ZONE}" \
    --version "template=${TEMPLATE}" \
    --type proactive --replacement-method recreate --max-surge 0 --max-unavailable 1
  gcloud compute instance-groups managed update "${VM_NAME}" --zone "${ZONE}" \
    --health-check "${HEALTH_CHECK}" --initial-delay "${HEALTH_INITIAL_DELAY}"
else
  gcloud compute instance-groups managed create "${VM_NAME}" --zone "${ZONE}" \
    --size 1 --template "${TEMPLATE}" \
    --health-check "${HEALTH_CHECK}" --initial-delay "${HEALTH_INITIAL_DELAY}"
fi

# 3. Drop templates from earlier rollouts (the group only references the new one).
gcloud compute instance-templates list --filter "name~^${VM_NAME}- AND name!=${TEMPLATE}" --format 'value(name)' \
  | xargs -r -n1 gcloud compute instance-templates delete --quiet 2>/dev/null || true

echo "Watcher MIG ${VM_NAME} now runs template ${TEMPLATE} (${IMAGE}:${TAG})"
