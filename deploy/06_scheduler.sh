#!/usr/bin/env bash
# Safety-net sweep: call /sync on a schedule even if the watcher is down.
source "$(dirname "$0")/_common.sh"
URL="$(service_url)"
JOB="mail-forwarder-sweep"

if gcloud scheduler jobs describe "${JOB}" --location "${REGION}" >/dev/null 2>&1; then
  action=update
else
  action=create
fi
gcloud scheduler jobs "${action}" http "${JOB}" \
  --location "${REGION}" \
  --schedule "${SWEEP_SCHEDULE}" \
  --uri "${URL}/sync" \
  --http-method POST \
  --oidc-service-account-email "${SCHEDULER_SA}" \
  --oidc-token-audience "${URL}" \
  --attempt-deadline 900s \
  --max-retry-attempts 0
