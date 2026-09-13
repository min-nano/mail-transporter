#!/usr/bin/env bash
# Deploy (or update) the Cloud Run forwarder service.
source "$(dirname "$0")/_common.sh"
TAG="${1:-latest}"
LABEL="${GMAIL_LABEL}"; [[ "${LABEL}" == "none" ]] && LABEL=""
# The service URL is only known once the service exists; on the very first
# deploy it is filled in with a second revision below. Until then the app
# refuses every /sync (it fails closed without EXPECTED_AUDIENCE), and the
# watcher cannot reach it anyway: its run.invoker binding is added last.
EXISTING_URL="$(service_url 2>/dev/null || true)"

gcloud run deploy "${SERVICE_NAME}" \
  --region "${REGION}" \
  --image "${IMAGE}:${TAG}" \
  --service-account "${RUN_SA}" \
  --no-allow-unauthenticated \
  --ingress all \
  --max-instances 1 \
  --min-instances 0 \
  --concurrency 1 \
  --cpu 1 --memory 512Mi \
  --timeout 900 \
  --set-env-vars "ICLOUD_USER=${ICLOUD_USER},GMAIL_LABEL=${LABEL},TIME_BUDGET_SECONDS=600,ALLOWED_INVOKER_SA=${WATCHER_SA},EXPECTED_AUDIENCE=${EXISTING_URL}" \
  --set-secrets "ICLOUD_PASSWORD=${SECRET_ICLOUD}:latest,GMAIL_OAUTH_JSON=${SECRET_GMAIL}:latest" \
  --quiet

if [[ -z "${EXISTING_URL}" ]]; then
  gcloud run services update "${SERVICE_NAME}" --region "${REGION}" \
    --update-env-vars "EXPECTED_AUDIENCE=$(service_url)" --quiet
fi

# Allow the watcher VM to invoke the service.
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" --region "${REGION}" \
  --member "serviceAccount:${WATCHER_SA}" --role roles/run.invoker --quiet >/dev/null
echo "Forwarder URL: $(service_url)"
