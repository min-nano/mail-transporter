#!/usr/bin/env bash
# Let GitHub Actions deploy without a service-account key: Workload Identity
# Federation trusts OIDC tokens from this repository's workflows and lets them
# act as a dedicated deployer service account. Run once as a project owner.
#
#   GITHUB_REPO=owner/repo ./deploy/06_github_deployer.sh
#
# Afterwards set these GitHub *repository variables* (Settings > Secrets and
# variables > Actions > Variables); the script prints the values:
#   GCP_WORKLOAD_IDENTITY_PROVIDER, GCP_DEPLOYER_SA, GCP_PROJECT_ID, ICLOUD_USER
#   (optional: GCP_REGION, GCP_ZONE, GMAIL_LABEL)
source "$(dirname "$0")/_common.sh"
: "${GITHUB_REPO:?set GITHUB_REPO=owner/repo}"

POOL="github"
PROVIDER="github"
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format 'value(projectNumber)')"
DEPLOYER_NAME="${DEPLOYER_SA%%@*}"

gcloud services enable iamcredentials.googleapis.com sts.googleapis.com >/dev/null

gcloud iam service-accounts describe "${DEPLOYER_SA}" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "${DEPLOYER_NAME}" --display-name "mail-transporter GitHub deployer"

# Workload identity pool + GitHub OIDC provider, restricted to this repository's main branch.
gcloud iam workload-identity-pools describe "${POOL}" --location global >/dev/null 2>&1 \
  || gcloud iam workload-identity-pools create "${POOL}" --location global --display-name "GitHub Actions"
if ! gcloud iam workload-identity-pools providers describe "${PROVIDER}" \
      --location global --workload-identity-pool "${POOL}" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers create-oidc "${PROVIDER}" \
    --location global --workload-identity-pool "${POOL}" \
    --display-name "GitHub" \
    --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition "assertion.repository == '${GITHUB_REPO}' && assertion.ref == 'refs/heads/main' && assertion.ref_type == 'branch'"
else
  gcloud iam workload-identity-pools providers update-oidc "${PROVIDER}" \
    --location global --workload-identity-pool "${POOL}" \
    --attribute-condition "assertion.repository == '${GITHUB_REPO}' && assertion.ref == 'refs/heads/main' && assertion.ref_type == 'branch'"
fi
# Only workflow runs on this repository's main branch can become the deployer;
# a workflow_dispatch from any other branch is refused at token exchange.
gcloud iam service-accounts add-iam-policy-binding "${DEPLOYER_SA}" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GITHUB_REPO}" \
  >/dev/null

# What a rollout (deploy/release.sh) needs, and nothing more.
for role in \
  roles/cloudbuild.builds.editor \
  roles/run.admin \
  roles/compute.instanceAdmin.v1 \
  roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member "serviceAccount:${DEPLOYER_SA}" --role "${role}" --condition=None >/dev/null
done
gcloud artifacts repositories add-iam-policy-binding "${AR_REPO}" --location "${REGION}" \
  --member "serviceAccount:${DEPLOYER_SA}" --role roles/artifactregistry.writer >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://${BUILD_BUCKET}" \
  --member "serviceAccount:${DEPLOYER_SA}" --role roles/storage.admin >/dev/null
# Deploying "as" the runtime service accounts requires actAs on each of them.
for sa in "${RUN_SA}" "${WATCHER_SA}"; do
  gcloud iam service-accounts add-iam-policy-binding "${sa}" \
    --member "serviceAccount:${DEPLOYER_SA}" --role roles/iam.serviceAccountUser >/dev/null
done

cat <<MSG

GitHub repository variables to set for ${GITHUB_REPO}:
  GCP_WORKLOAD_IDENTITY_PROVIDER = projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}
  GCP_DEPLOYER_SA                = ${DEPLOYER_SA}
  GCP_PROJECT_ID                 = ${PROJECT_ID}
  ICLOUD_USER                    = ${ICLOUD_USER}
  GCP_REGION (optional)          = ${REGION}
  GCP_ZONE (optional)            = ${ZONE}
  GMAIL_LABEL (optional)         = ${GMAIL_LABEL}
MSG
