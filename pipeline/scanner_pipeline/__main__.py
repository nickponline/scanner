"""python -m scanner_pipeline <stage> --scan ID --version N [--config JSON]"""
import argparse
import importlib
import json
import logging
import sys

from . import STAGES
from .ctx import StageContext


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=STAGES)
    ap.add_argument("--scan", required=True)
    ap.add_argument("--version", type=int, required=True)
    ap.add_argument("--config", default="{}")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    cfg = json.loads(a.config) if a.config else {}
    mod = importlib.import_module(f"scanner_pipeline.stages.{a.stage}")
    StageContext(stage=a.stage, scan_id=a.scan, version=a.version, config=cfg).run(mod.run)


if __name__ == "__main__":
    main()
