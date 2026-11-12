"""Stage context: local workdir, GCS IO, Firestore status, stage.json.

Layout (CONTRACTS.md / REQUIREMENTS.md §4):
  gs://BUCKET/raw/{scan}/...                 capture package (immutable)
  gs://BUCKET/work/{scan}/v{NN}/{stage}/...  stage outputs
  gs://BUCKET/processed/{scan}/v{NN}/...     published artefacts

Inputs from an earlier stage are resolved through Firestore `stage_versions[stage]`, so a
reprocess that starts at stage K (new version) reads stages < K from the version that produced them.
Set SCANNER_LOCAL=1 to run against a local directory tree (SCANNER_LOCAL_ROOT) instead of GCS/Firestore.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("scanner")

BUCKET = os.environ.get("SCANNER_BUCKET", "scanner-514023-data")
LOCAL = os.environ.get("SCANNER_LOCAL") == "1"
LOCAL_ROOT = Path(os.environ.get("SCANNER_LOCAL_ROOT", "/tmp/scanner-local"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def git_sha() -> str:
    return os.environ.get("SCANNER_GIT_SHA", "dev")


# ----------------------------------------------------------------------------- storage
class Store:
    """Minimal blob store over GCS or a local directory (for tests)."""

    def __init__(self):
        if LOCAL:
            self.client = None
        else:
            from google.cloud import storage

            self.client = storage.Client()
            self.bucket = self.client.bucket(BUCKET)

    def list(self, prefix: str) -> list[str]:
        if LOCAL:
            root = LOCAL_ROOT / prefix
            if not root.exists():
                return []
            return [str(p.relative_to(LOCAL_ROOT)) for p in root.rglob("*") if p.is_file()]
        return [b.name for b in self.client.list_blobs(BUCKET, prefix=prefix)]

    def exists(self, key: str) -> bool:
        if LOCAL:
            return (LOCAL_ROOT / key).is_file()
        return self.bucket.blob(key).exists()

    def download(self, key: str, dst: Path) -> Path:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if LOCAL:
            shutil.copyfile(LOCAL_ROOT / key, dst)
        else:
            self.bucket.blob(key).download_to_filename(str(dst))
        return dst

    def download_prefix(self, prefix: str, dst: Path, workers: int = 10) -> list[Path]:
        prefix = prefix.rstrip("/") + "/"
        keys = self.list(prefix)
        out = [dst / k[len(prefix):] for k in keys]
        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(lambda kv: self.download(kv[0], kv[1]), zip(keys, out)))
        return out

    def upload(self, src: Path, key: str, content_type: str | None = None, cache: str | None = None):
        if LOCAL:
            p = LOCAL_ROOT / key
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, p)
            return
        b = self.bucket.blob(key)
        if cache:
            b.cache_control = cache
        b.upload_from_filename(str(src), content_type=content_type or guess_type(str(src)))

    def upload_dir(self, src: Path, prefix: str, workers: int = 10):
        files = [p for p in src.rglob("*") if p.is_file()]
        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(lambda p: self.upload(p, f"{prefix.rstrip('/')}/{p.relative_to(src)}"), files))

    def read_json(self, key: str):
        if LOCAL:
            return json.loads((LOCAL_ROOT / key).read_text())
        return json.loads(self.bucket.blob(key).download_as_bytes())

    def write_json(self, key: str, obj):
        data = json.dumps(obj, indent=1)
        if LOCAL:
            p = LOCAL_ROOT / key
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(data)
        else:
            self.bucket.blob(key).upload_from_string(data, content_type="application/json")

    def crc32c(self, key: str) -> tuple[int, str] | None:
        if LOCAL:
            p = LOCAL_ROOT / key
            if not p.exists():
                return None
            from .crc import crc32c_b64

            return p.stat().st_size, crc32c_b64(p.read_bytes())
        b = self.bucket.get_blob(key)
        return None if b is None else (b.size, b.crc32c)


def guess_type(path: str) -> str:
    import mimetypes

    ext = path.rsplit(".", 1)[-1].lower()
    return {
        "rad": "application/octet-stream", "spz": "application/octet-stream", "ply": "application/octet-stream",
        "glb": "model/gltf-binary", "svg": "image/svg+xml", "json": "application/json", "jpg": "image/jpeg",
        "png": "image/png",
    }.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"


# ----------------------------------------------------------------------------- status
class Status:
    def __init__(self, scan_id: str):
        self.scan_id = scan_id
        if LOCAL:
            self.path = LOCAL_ROOT / "firestore" / f"{scan_id}.json"
            self.path.parent.mkdir(parents=True, exist_ok=True)
        else:
            from google.cloud import firestore

            self.db = firestore.Client()
            self.doc = self.db.collection("scans").document(scan_id)

    def get(self) -> dict:
        if LOCAL:
            return json.loads(self.path.read_text()) if self.path.exists() else {}
        snap = self.doc.get()
        return snap.to_dict() or {}

    def update(self, fields: dict):
        """Dotted-key merge update (Firestore semantics)."""
        if LOCAL:
            d = self.get()
            for k, v in fields.items():
                cur = d
                parts = k.split(".")
                for p in parts[:-1]:
                    cur = cur.setdefault(p, {})
                cur[parts[-1]] = v
            self.path.write_text(json.dumps(d, indent=1, default=str))
        else:
            self.doc.update(fields)


# ----------------------------------------------------------------------------- context
@dataclass
class StageContext:
    stage: str
    scan_id: str
    version: int
    config: dict = field(default_factory=dict)
    root: Path = field(default_factory=lambda: Path(os.environ.get("SCANNER_WORKDIR", "/work")))

    def __post_init__(self):
        self.store = Store()
        self.status = Status(self.scan_id)
        self.work = self.root / self.scan_id
        self.out = self.work / self.stage
        if self.out.exists():
            shutil.rmtree(self.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.metrics: dict = {}
        self._doc = self.status.get()

    # GCS keys
    @property
    def raw_prefix(self) -> str:
        return f"raw/{self.scan_id}"

    def work_prefix(self, stage: str, version: int | None = None) -> str:
        if version is None:
            version = self.version if stage == self.stage else self.stage_version(stage)
        return f"work/{self.scan_id}/v{version:02d}/{stage}"

    @property
    def processed_prefix(self) -> str:
        return f"processed/{self.scan_id}/v{self.version:02d}"

    def stage_version(self, stage: str) -> int:
        # config.stage_versions pins inputs for this run (e.g. re-publish against older frames while a newer
        # reprocess is mid-flight and has already advanced the scan's stage_versions map)
        v = (self.config.get("stage_versions") or {}).get(stage)
        if v is None:
            v = (self._doc.get("stage_versions") or {}).get(stage)
        return int(v) if v is not None else self.version

    def has_stage(self, stage: str) -> bool:
        """True if the stage completed successfully for the version we'd read from."""
        return self.store.exists(f"{self.work_prefix(stage)}/stage.json")

    def fetch_stage(self, stage: str, subpath: str = "") -> Path:
        """Download (all or part of) an earlier stage's outputs into work/<stage>/."""
        dst = self.work / stage
        prefix = self.work_prefix(stage) + (f"/{subpath}" if subpath else "")
        if subpath and not subpath.endswith("/") and "." in subpath.rsplit("/", 1)[-1]:
            self.store.download(prefix, dst / subpath)
            return dst / subpath
        self.store.download_prefix(prefix, dst / subpath if subpath else dst)
        return dst

    def fetch_raw(self, rel: str) -> Path:
        return self.store.download(f"{self.raw_prefix}/{rel}", self.work / "raw" / rel)

    def manifest(self) -> dict:
        p = self.work / "raw" / "manifest.json"
        if not p.exists():
            self.fetch_raw("manifest.json")
        return json.loads(p.read_text())

    def progress(self, frac: float, note: str = ""):
        self.status.update({f"stages.{self.stage}.progress": round(float(frac), 3),
                            f"stages.{self.stage}.note": note, "progress_note": note})

    def run(self, fn):
        t0 = time.time()
        self.status.update({
            "status": "processing", "stage": self.stage,
            f"stages.{self.stage}": {"status": "running", "started_at": now_iso(), "version": self.version,
                                     "progress": 0.0},
        })
        try:
            fn(self)
        except Exception as e:  # noqa: BLE001
            log.exception("stage %s failed", self.stage)
            self.status.update({
                f"stages.{self.stage}.status": "failed", f"stages.{self.stage}.ended_at": now_iso(),
                f"stages.{self.stage}.error": f"{type(e).__name__}: {e}"[:2000],
                f"stages.{self.stage}.traceback": traceback.format_exc()[-6000:],
            })
            raise
        dt = time.time() - t0
        info = {
            "stage": self.stage, "scan_id": self.scan_id, "version": self.version, "config": self.config,
            "git_sha": git_sha(), "image": os.environ.get("SCANNER_IMAGE", ""), "duration_s": dt,
            "metrics": self.metrics, "finished_at": now_iso(),
        }
        (self.out / "stage.json").write_text(json.dumps(info, indent=1, default=str))
        self.store.upload_dir(self.out, self.work_prefix(self.stage))
        self.status.update({
            f"stages.{self.stage}.status": "done", f"stages.{self.stage}.ended_at": now_iso(),
            f"stages.{self.stage}.duration_s": round(dt, 1), f"stages.{self.stage}.metrics": self.metrics,
            f"stages.{self.stage}.progress": 1.0, f"stage_versions.{self.stage}": self.version,
        })
        log.info("stage %s done in %.1fs metrics=%s", self.stage, dt, self.metrics)


def sh(cmd: list[str] | str, cwd: Path | None = None, env: dict | None = None, log_file: Path | None = None) -> str:
    """Run a command, stream output to the log, raise with the tail on failure."""
    shell = isinstance(cmd, str)
    log.info("$ %s", cmd if shell else " ".join(map(str, cmd)))
    e = dict(os.environ, **(env or {}))
    p = subprocess.Popen(cmd, cwd=cwd, env=e, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1)
    lines: list[str] = []
    fh = open(log_file, "a") if log_file else None
    for line in p.stdout:  # type: ignore[union-attr]
        lines.append(line)
        if len(lines) > 4000:
            del lines[:2000]
        print(line, end="", flush=True)
        if fh:
            fh.write(line)
    p.wait()
    if fh:
        fh.close()
    if p.returncode != 0:
        tail = "".join(lines[-40:])
        raise RuntimeError(f"command failed ({p.returncode}): {cmd if shell else ' '.join(map(str, cmd))}\n{tail}")
    return "".join(lines)
