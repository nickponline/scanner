#!/usr/bin/env bash
# Build pipeline images on the scanner-builder VM (Cloud Build has no capacity on this project yet).
# usage: scripts/build_images.sh [cpu colmap gpu da3 come]   (default: all)
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
source scripts/env.sh
IMAGES=${*:-cpu colmap gpu da3}
TAG=${TAG:-$(date +%Y%m%d-%H%M)}
VM=scanner-builder
# a VM still STOPPING from the previous run can't be started; wait, then start and wait for sshd
while [ "$(gcloud compute instances describe $VM --zone $ZONE --format='value(status)')" = STOPPING ]; do sleep 5; done
gcloud compute instances start $VM --zone $ZONE -q >/dev/null 2>&1 || true
for i in $(seq 1 30); do gcloud compute ssh $VM --zone $ZONE -q --command true >/dev/null 2>&1 && break; sleep 5; done
tar --exclude node_modules --exclude .venv --exclude .git --exclude 'viewer/public/fixtures' --exclude 'viewer/test-screenshots' \
    --exclude mobile --exclude infra/.terraform -czf /tmp/scanner-src.tgz pipeline viewer/scripts
gcloud compute scp /tmp/scanner-src.tgz $VM:/tmp/scanner-src.tgz --zone $ZONE -q
rc=0
gcloud compute ssh $VM --zone $ZONE -q --command "
set -e
rm -f /tmp/build-failed-*
while [ ! -f /var/run/builder-ready ]; do sleep 5; done
sudo rm -rf /build && sudo mkdir -p /build && sudo tar -xzf /tmp/scanner-src.tgz -C /build
cd /build
pids=''
for img in $IMAGES; do
  (sudo docker build -f pipeline/docker/\$img.Dockerfile -t $AR_REPO/\$img:$TAG -t $AR_REPO/\$img:latest . > /tmp/build-\$img.log 2>&1 \
     && sudo docker push -q $AR_REPO/\$img:$TAG >> /tmp/build-\$img.log 2>&1 && sudo docker push -q $AR_REPO/\$img:latest >> /tmp/build-\$img.log 2>&1 \
     && echo \"\$img OK\" || (echo \"\$img FAILED\"; tail -40 /tmp/build-\$img.log; touch /tmp/build-failed-\$img)) &
  pids=\"\$pids \$!\"
done
wait \$pids
! ls /tmp/build-failed-* >/dev/null 2>&1
" || rc=1
echo "tag: $TAG"
echo "stopping builder VM (shares the 32-vCPU project quota with pipeline jobs)"
gcloud compute instances stop $VM --zone $ZONE -q --async >/dev/null 2>&1 || true
exit $rc
