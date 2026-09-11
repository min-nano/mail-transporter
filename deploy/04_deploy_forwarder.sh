#!/usr/bin/env bash
# Deploy (or update) the Cloud Run forwarder service.
source "$(dirname "$0")/_common.sh"
TAG="${1:-latest}"
LABEL="${GMAIL_LABEL}"; [[ "${LABEL}" == "none" ]] && LABEL=""

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
  --set-env-vars "ICLOUD_USER=${ICLOUD_USER},GMAIL_LABEL=${LABEL},TIME_BUDGET_SECONDS=600,ALLOWED_INVOKER_SA=${WATCHER_SA}" \
  --set-secrets "ICLOUD_PASSWORD=${SECRET_ICLOUD}:latest,GMAIL_OAUTH_JSON=${SECRET_GMAIL}:latest" \
  --quiet

# Allow the watcher VM to invoke the service.
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" --region "${REGION}" \
  --member "serviceAccount:${WATCHER_SA}" --role roles/run.invoker --quiet >/dev/null
echo "Forwarder URL: $(service_url)"
