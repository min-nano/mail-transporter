#!/usr/bin/env bash
# Deploy (or update) the Cloud Run forwarder service.
source "$(dirname "$0")/_common.sh"
TAG="${1:-latest}"

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
  --set-env-vars "ICLOUD_USER=${ICLOUD_USER},GMAIL_LABEL=${GMAIL_LABEL},TIME_BUDGET_SECONDS=600" \
  --set-secrets "ICLOUD_PASSWORD=${SECRET_ICLOUD}:latest,GMAIL_OAUTH_JSON=${SECRET_GMAIL}:latest" \
  --quiet

# Allow the watcher VM and Cloud Scheduler to invoke the service.
for sa in "${WATCHER_SA}" "${SCHEDULER_SA}"; do
  gcloud run services add-iam-policy-binding "${SERVICE_NAME}" --region "${REGION}" \
    --member "serviceAccount:${sa}" --role roles/run.invoker --quiet >/dev/null
done
echo "Forwarder URL: $(service_url)"
