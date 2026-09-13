# Copy to deploy/env.sh (git-ignored) and fill in. Only the first block is
# required; the rest override the defaults in deploy/_common.sh.
export PROJECT_ID="my-gcp-project"
export ICLOUD_USER="you@icloud.com"  # iCloud Mail address (Apple ID)

export REGION="us-central1"          # Cloud Run / Artifact Registry
export ZONE="us-central1-a"          # e2-micro free tier: us-west1, us-central1, us-east1 only
export GMAIL_LABEL="iCloud"          # Gmail label added to forwarded mail ("none" to disable)

# GitHub repository for the automated deploy (deploy/06_github_deployer.sh)
export GITHUB_REPO="your-org/mail-transporter"

# Names (rarely need changing; see deploy/_common.sh for the defaults)
# export SERVICE_NAME="mail-forwarder"
# export VM_NAME="mail-watcher"
# export AR_REPO="mail-transporter"
# export HEALTH_PORT="8080"           # watcher /healthz port (MIG autohealing)
# export HEALTH_INITIAL_DELAY="300"   # seconds a fresh VM gets before autohealing judges it
