#!/usr/bin/env bash
# Build the API (+ bundled viewer) on the builder VM and deploy to Cloud Run.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
source scripts/env.sh
TAG=${TAG:-$(date +%Y%m%d-%H%M%S)}
IMG=$AR_REPO/api:$TAG
VM=scanner-builder
# a VM still STOPPING from the previous run can't be started; wait, then start and wait for sshd
while [ "$(gcloud compute instances describe $VM --zone $ZONE --format='value(status)')" = STOPPING ]; do sleep 5; done
gcloud compute instances start $VM --zone $ZONE -q >/dev/null 2>&1 || true
for i in $(seq 1 30); do gcloud compute ssh $VM --zone $ZONE -q --command true >/dev/null 2>&1 && break; sleep 5; done
tar --exclude node_modules --exclude .venv --exclude 'viewer/public/fixtures' --exclude 'viewer/test-screenshots' \
    --exclude 'viewer/dist' -czf /tmp/scanner-api-src.tgz api viewer
gcloud compute scp /tmp/scanner-api-src.tgz $VM:/tmp/scanner-api-src.tgz --zone $ZONE -q
gcloud compute ssh $VM --zone $ZONE -q --command "
set -e
sudo rm -rf /build-api && sudo mkdir -p /build-api && sudo tar -xzf /tmp/scanner-api-src.tgz -C /build-api
cd /build-api && sudo docker build -f api/Dockerfile -t $IMG . > /tmp/build-api.log 2>&1 || (tail -40 /tmp/build-api.log; exit 1)
sudo docker push -q $IMG
"
gcloud run deploy scanner-api --image $IMG --region $REGION --service-account scanner-api@$PROJECT.iam.gserviceaccount.com \
  --no-invoker-iam-check --memory 1Gi --cpu 1 --min-instances 0 --max-instances 10 --timeout 300 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT,SCANNER_BUCKET=$BUCKET,REGION=$REGION,PIPELINE_IMAGE_TAG=${PIPELINE_IMAGE_TAG:-latest},AUTH_DISABLED=${AUTH_DISABLED:-1}" -q 2>&1 | tail -3
gcloud compute instances stop $VM --zone $ZONE -q --async >/dev/null 2>&1 || true
