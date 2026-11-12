# Gaussian splat + LOD are disabled for now (code kept in stages/splat*.py, stages/lod.py); re-add "splat", "lod"
# after "densify" here, in api/app/main.py, in workflows/pipeline.yaml and in publish.SPLAT_ENABLED to restore them.
STAGES = ["ingest", "frames", "sfm", "depth", "densify", "plan", "publish"]
