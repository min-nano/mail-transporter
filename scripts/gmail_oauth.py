#!/usr/bin/env python3
"""One-time helper: obtain a Gmail refresh token for the forwarder.

Usage:
    pip install google-auth-oauthlib
    python scripts/gmail_oauth.py client_secret.json > gmail-oauth.json

Then store gmail-oauth.json in Secret Manager (see deploy/02_secrets.sh).
The OAuth client must be of type "Desktop app", and the consent screen must
be published ("In production"); refresh tokens issued while the app is in
"Testing" expire after 7 days.
"""

from __future__ import annotations

import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/gmail.labels",
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], scopes=SCOPES)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not credentials.refresh_token:
        print("No refresh token returned; revoke the app's access and try again.", file=sys.stderr)
        return 1
    json.dump(
        {
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "refresh_token": credentials.refresh_token,
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
