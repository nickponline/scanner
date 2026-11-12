# Scanner

Walk through a home with an iPhone Pro (LiDAR) → cloud reconstruction → web viewer with Gaussian splat,
CoMe mesh, Argon-generated floor plan + architectural model, and click-through photos.

- Requirements: [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)
- Interface contracts (capture format, API, outputs, frames): [docs/CONTRACTS.md](docs/CONTRACTS.md)

```
mobile/     Expo app + Swift native module (ARKit capture, background upload)      → mobile/README.md
api/        FastAPI on Cloud Run: scans, resumable uploads, workflow trigger, signed assets, serves viewer
viewer/     Vite + three.js + Spark 2 web viewer                                    → viewer/README.md
pipeline/   Python stages + Docker images + Cloud Workflows definition
infra/      Terraform (GCP project scanner-514023)
tools/      arkitscenes_to_capture.py (test data), upload_capture.py (CLI uploader = app upload flow)
scripts/    env.sh, build_images.sh, deploy_api.sh
```

## Pipeline

| # | stage | image | machine | what |
|---|---|---|---|---|
| 1 | ingest | cpu | n2-standard-4 | verify upload (size/CRC32C), QA report |
| 2 | frames | cpu | n2-highcpu-16 | pose-gated sharp keyframes, LiDAR pairing, hero stills |
| 3 | sfm | colmap | g2 + L4 | COLMAP 4.2.1: ALIKED+LightGlue, sequential+spatial matching, ARKit pose/gravity priors, global mapper (→ pose-prior incremental fallback), Sim(3) to ARKit (metric) |
| 4 | depth | cpu | n2-highmem-8 | LiDAR filtering, TSDF fusion, floor/ceiling, Manhattan World frame, consensus-filtered depth |
| 5 | splat | gpu | g2 + L4 | gsplat 1.5.3 MCMC, LiDAR log-depth loss, per-image affine colour, antialiased, held-out PSNR |
| 6 | lod | cpu | n2-highmem-16 | Spark `build-lod --quality` → scene.rad, scene.spz |
| 7 | mesh | come | g2 + L4 | CoMe (ScanNet++ settings) → marching tets; TSDF fallback; gltfpack |
| 8 | plan | cpu | n2-standard-8 | Gemini 4 Argon agent loop → building_model.json, GLBs, plan SVGs |
| 9 | publish | cpu | n2-standard-8 | photos (upright), cameras/trajectory, scene.json, status=ready, push |

Stages 1–4 and 9 are fatal; 5–8 are optional (the scan publishes with whatever layers succeeded).

## Common tasks

```bash
source scripts/env.sh                       # uses gcloud config "scanner" (nick@liminal.xyz) — never your default
tf plan / tf apply                          # infra (state in gs://scanner-514023-tfstate)
scripts/build_images.sh [cpu colmap gpu come]   # builds on the scanner-builder VM, pushes :latest + :TAG
scripts/deploy_api.sh                       # API + viewer → Cloud Run (scanner-api)

# owner API token
gcloud secrets versions access latest --secret=api-tokens

# end-to-end test with a real LiDAR capture (ARKitScenes)
python tools/arkitscenes_to_capture.py <ARKitScenes raw dir> /tmp/cap
python tools/upload_capture.py /tmp/cap --api https://<run-url> --token <token>

# re-run from a stage (new version), e.g. only re-plan
curl -X POST $API/v1/scans/$SCAN/reprocess -H "Authorization: Bearer $TOKEN" -d '{"from_stage":"plan"}' -H 'content-type: application/json'

# run a stage locally against a local store (no GCP)
cd pipeline && SCANNER_LOCAL=1 SCANNER_LOCAL_ROOT=/tmp/store SCANNER_WORKDIR=/tmp/work \
  .venv/bin/python -m scanner_pipeline depth --scan scan_x --version 1
```

## Known constraints (Nov 2026)
- GPU quota: 1 GPU project-wide (L4). A100 requests were auto-denied for this new project — request in the console
  (IAM & Admin → Quotas: `GPUS_ALL_REGIONS`, `NVIDIA_A100_80GB_GPUS` us-central1) once there is billing history.
- CPU quota is 32 vCPUs project-wide and SSD 500 GB (increase requests auto-capped). The builder VM is
  n2-standard-16 so a build fits next to one 16-vCPU pipeline job; `build_images.sh`/`deploy_api.sh` start it and
  stop it afterwards. Batch jobs use pd-standard boot disks for the same reason.
- Cloud Build can't run on this project yet (queue never starts) → images build on the `scanner-builder` VM
  (stop it when idle: `gcloud compute instances stop scanner-builder --zone us-central1-a`).
- CoMe is under the Inria non-commercial licence (research/personal use only).
