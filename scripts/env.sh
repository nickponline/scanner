# Source me: per-repo gcloud config so we never touch the user's default gcloud account.
export CLOUDSDK_ACTIVE_CONFIG_NAME=scanner
export PROJECT=scanner-514023
export REGION=us-central1
export ZONE=us-central1-a
export BUCKET=scanner-514023-data
export AR_REPO=$REGION-docker.pkg.dev/$PROJECT/scanner
export PATH="$HOME/.local/bin:$PATH"
tf() { GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token) terraform -chdir="$(git rev-parse --show-toplevel)/infra" "$@"; }
