#!/usr/bin/env bash
# Enable the Google Cloud APIs this project needs.
source "$(dirname "$0")/_common.sh"

gcloud services enable \
  compute.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  gmail.googleapis.com \
  iam.googleapis.com \
  logging.googleapis.com
