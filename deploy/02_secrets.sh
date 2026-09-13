#!/usr/bin/env bash
# Store the iCloud app-specific password and the Gmail OAuth JSON in Secret Manager.
#
#   ./deploy/02_secrets.sh icloud   # prompts for the app-specific password
#   ./deploy/02_secrets.sh gmail gmail-oauth.json   # output of scripts/gmail_oauth.py
source "$(dirname "$0")/_common.sh"

ensure_secret() {
  gcloud secrets describe "$1" >/dev/null 2>&1 || gcloud secrets create "$1" --replication-policy automatic
}

case "${1:-}" in
  icloud)
    ensure_secret "${SECRET_ICLOUD}"
    read -r -s -p "iCloud app-specific password: " password; echo
    printf '%s' "${password}" | gcloud secrets versions add "${SECRET_ICLOUD}" --data-file=-
    for sa in "${RUN_SA}" "${WATCHER_SA}"; do
      gcloud secrets add-iam-policy-binding "${SECRET_ICLOUD}" \
        --member "serviceAccount:${sa}" --role roles/secretmanager.secretAccessor >/dev/null
    done
    ;;
  gmail)
    [[ -f "${2:-}" ]] || { echo "usage: $0 gmail <gmail-oauth.json>" >&2; exit 1; }
    ensure_secret "${SECRET_GMAIL}"
    gcloud secrets versions add "${SECRET_GMAIL}" --data-file="$2"
    gcloud secrets add-iam-policy-binding "${SECRET_GMAIL}" \
      --member "serviceAccount:${RUN_SA}" --role roles/secretmanager.secretAccessor >/dev/null
    ;;
  *)
    echo "usage: $0 icloud | gmail <gmail-oauth.json>" >&2; exit 1 ;;
esac
# Destroy every version but the newest. The free tier counts 6 *active* versions
# and a disabled version is still active (and still recoverable by whoever can
# re-enable it); only destroyed versions leave the count and the old credential.
name="${SECRET_ICLOUD}"; [[ "$1" == gmail ]] && name="${SECRET_GMAIL}"
gcloud secrets versions list "${name}" --sort-by '~createTime' --filter 'state!=destroyed' \
  --format 'value(name)' | tail -n +2 \
  | xargs -r -I{} gcloud secrets versions destroy {} --secret "${name}" --quiet >/dev/null

# Both consumers read the secret once at start-up, so tell the operator what
# still has to be restarted (see README, "シークレットのローテーション後の反映").
if [[ "$1" == icloud ]]; then
  echo "Stored. Roll out the new password with ./deploy/04_deploy_forwarder.sh and ./deploy/05_deploy_watcher.sh"
else
  echo "Stored. Roll out the new token with ./deploy/04_deploy_forwarder.sh"
fi
