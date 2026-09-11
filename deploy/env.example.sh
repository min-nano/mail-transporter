# Copy to deploy/env.sh (git-ignored) and fill in.
export PROJECT_ID="my-gcp-project"
export REGION="us-central1"          # Cloud Run / Artifact Registry / Scheduler
export ZONE="us-central1-a"          # e2-micro free tier: us-west1, us-central1, us-east1 only
export FIRESTORE_LOCATION="nam5"

export ICLOUD_USER="you@icloud.com"  # iCloud Mail address (Apple ID)
export GMAIL_LABEL="iCloud"          # Gmail label added to forwarded mail ("" to disable)

# Names (rarely need changing)
export SERVICE_NAME="mail-forwarder"
export VM_NAME="mail-watcher"
export AR_REPO="mail-transporter"
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/mail-transporter"
export RUN_SA="mail-forwarder@${PROJECT_ID}.iam.gserviceaccount.com"
export WATCHER_SA="mail-watcher@${PROJECT_ID}.iam.gserviceaccount.com"
export SCHEDULER_SA="mail-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
export SECRET_ICLOUD="icloud-password"
export SECRET_GMAIL="gmail-oauth"
export SWEEP_SCHEDULE="*/30 * * * *"  # Cloud Scheduler safety-net sweep
