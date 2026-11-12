"""Scanner API (CONTRACTS.md §3) — FastAPI on Cloud Run. Also serves the viewer SPA."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

log = logging.getLogger("scanner.api")
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "scanner-514023")
REGION = os.environ.get("REGION", "us-central1")
BUCKET = os.environ.get("SCANNER_BUCKET", f"{PROJECT}-data")
WORKFLOW = os.environ.get("WORKFLOW", "scanner-pipeline")
IMAGE_TAG = os.environ.get("PIPELINE_IMAGE_TAG", "latest")
VIEWER_DIR = Path(os.environ.get("VIEWER_DIR", "/app/viewer"))
STAGES = ["ingest", "frames", "sfm", "depth", "densify", "plan", "publish"]  # splat + lod disabled for now
ASSET_TTL = 12 * 3600

app = FastAPI(title="Scanner API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ----------------------------------------------------------------------------- clients
@lru_cache
def gcs():
    from google.cloud import storage

    return storage.Client(project=PROJECT)


@lru_cache
def db():
    from google.cloud import firestore

    return firestore.Client(project=PROJECT)


_secret_cache: dict[str, tuple[float, str]] = {}


def secret(name: str) -> str:
    hit = _secret_cache.get(name)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    if os.environ.get(f"SECRET_{name.upper().replace('-', '_')}"):
        val = os.environ[f"SECRET_{name.upper().replace('-', '_')}"]
    else:
        from google.cloud import secretmanager

        c = secretmanager.SecretManagerServiceClient()
        val = c.access_secret_version(name=f"projects/{PROJECT}/secrets/{name}/versions/latest").payload.data.decode()
    _secret_cache[name] = (time.time(), val)
    return val


def signing_creds():
    """Credentials + SA email usable for V4 signing via IAM signBlob (no key file on Cloud Run)."""
    import google.auth
    from google.auth.transport import requests as gr

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if not creds.valid:
        creds.refresh(gr.Request())
    email = getattr(creds, "service_account_email", None) or os.environ.get("SIGNER_EMAIL")
    return creds, email


def sign_get(key: str, ttl: int = ASSET_TTL) -> str:
    creds, email = signing_creds()
    blob = gcs().bucket(BUCKET).blob(key)
    kw: dict[str, Any] = {"version": "v4", "expiration": timedelta(seconds=ttl), "method": "GET"}
    if email and email != "default":
        kw.update(service_account_email=email, access_token=creds.token)  # IAM signBlob
    return blob.generate_signed_url(**kw)


# ----------------------------------------------------------------------------- auth
def owner_from_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    tok = authorization.split(" ", 1)[1].strip()
    try:
        tokens = json.loads(secret("api-tokens"))
    except Exception:  # noqa: BLE001
        log.exception("cannot read api-tokens")
        raise HTTPException(500, "auth backend unavailable")
    for t, email in tokens.items():
        if hmac.compare_digest(t, tok):
            return email
    return None


# Temporary open mode for testing (AUTH_DISABLED=1): requests without a valid token act as the first owner in
# api-tokens, so the app and the web index work with no token. Unset the env var to require tokens again.
AUTH_DISABLED = os.environ.get("AUTH_DISABLED") == "1"


def require_user(authorization: str | None = Header(default=None)) -> str:
    who = owner_from_token(authorization) if authorization else None
    if not who and AUTH_DISABLED:
        return next(iter(json.loads(secret("api-tokens")).values()))
    if not who:
        raise HTTPException(401, "invalid or missing bearer token")
    return who


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(scan_id: str, ttl: int | None, kind: str) -> str:
    body = {"s": scan_id, "k": kind, "e": int(time.time() + ttl) if ttl else 0, "n": secrets.token_hex(4)}
    raw = _b64(json.dumps(body, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret("share-signing-key").encode(), raw.encode(), hashlib.sha256).digest()[:18])
    return f"{raw}.{sig}"


def check_token(tok: str, scan_id: str, kinds=("asset", "share")) -> bool:
    try:
        raw, sig = tok.split(".", 1)
        good = _b64(hmac.new(secret("share-signing-key").encode(), raw.encode(), hashlib.sha256).digest()[:18])
        if not hmac.compare_digest(good, sig):
            return False
        body = json.loads(_unb64(raw))
        if body["s"] != scan_id or body["k"] not in kinds:
            return False
        if body["e"] and body["e"] < time.time():
            return False
        if body["k"] == "share":
            snap = db().collection("scans").document(scan_id).get()
            if not snap.exists or tok not in (snap.to_dict().get("share_tokens") or []):
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------------------- helpers
def new_scan_id() -> str:
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    t = int(time.time() * 1000)
    s = ""
    for _ in range(10):
        s = alphabet[t % 32] + s
        t //= 32
    return "scan_" + s + "".join(secrets.choice(alphabet) for _ in range(8))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def scan_doc(scan_id: str, owner: str | None = None) -> dict:
    snap = db().collection("scans").document(scan_id).get()
    if not snap.exists:
        raise HTTPException(404, "scan not found")
    d = snap.to_dict()
    if owner is not None and d.get("owner") != owner:
        raise HTTPException(404, "scan not found")
    return d


def progress_of(d: dict) -> float:
    st = d.get("stages") or {}
    if d.get("status") == "ready":
        return 1.0
    done = sum(1 for s in STAGES if (st.get(s) or {}).get("status") == "done" and (st.get(s) or {}).get("version") == d.get("processing_version"))
    cur = (st.get(d.get("stage") or "") or {}).get("progress") or 0
    return round(min(0.999, (done + cur) / len(STAGES)), 3)


def public_scan(d: dict, request: Request, full=False) -> dict:
    base = str(request.base_url).rstrip("/")
    if request.headers.get("x-forwarded-proto") == "https":
        base = base.replace("http://", "https://", 1)
    out = {k: d.get(k) for k in ["scan_id", "name", "created_at", "status", "stage", "current_version", "versions",
                                 "partial", "failed_stages", "progress_note", "failed_stage", "processing_version"]}
    out["progress"] = progress_of(d)
    # compact per-stage state for the index page: done | running | failed | pending (for the version being processed)
    st, ver = d.get("stages") or {}, d.get("processing_version") or d.get("current_version")
    out["stage_states"] = {}
    for s_ in STAGES:
        x = st.get(s_) or {}
        state = x.get("status") or "pending"
        # a stage done in an earlier version still counts (reprocess runs reuse earlier outputs);
        # a stale running/failed state from another version does not
        if state != "done" and ver is not None and x.get("version") not in (None, ver):
            state = "pending"
        out["stage_states"][s_] = {"processing": "running"}.get(state, state)
    out["viewer_url"] = f"{base}{d['viewer_path']}" if d.get("viewer_path") else None
    out["thumbnail_url"] = None
    if d.get("thumb"):
        try:
            out["thumbnail_url"] = sign_get(d["thumb"], 6 * 3600)
        except Exception:  # noqa: BLE001
            log.exception("thumb sign failed")
    if full:
        st = d.get("stages") or {}
        out["stages"] = {s: {k: v for k, v in (st.get(s) or {}).items() if k != "traceback"} for s in STAGES if s in st}
        out["qa"] = d.get("qa")
        out["upload"] = d.get("upload")
    return out


def start_workflow(scan_id: str, version: int, from_stage="ingest", config: dict | None = None, only=None) -> str:
    from google.cloud.workflows import executions_v1

    c = executions_v1.ExecutionsClient()
    arg = {"scan_id": scan_id, "version": version, "from_stage": from_stage, "config": config or {}, "image_tag": IMAGE_TAG}
    if only:
        arg["only"] = only
    ex = c.create_execution(parent=f"projects/{PROJECT}/locations/{REGION}/workflows/{WORKFLOW}",
                            execution=executions_v1.Execution(argument=json.dumps(arg)))
    return ex.name


# ----------------------------------------------------------------------------- models
class CreateScan(BaseModel):
    manifest: dict


class Paths(BaseModel):
    paths: list[str]


class Reprocess(BaseModel):
    from_stage: str = "ingest"
    config: dict | None = None
    only: list[str] | None = None


class PushToken(BaseModel):
    expo_push_token: str


# ----------------------------------------------------------------------------- routes
@app.get("/v1/health")
def healthz():
    return {"ok": True}


def _session(key: str, size: int, ctype: str | None) -> str:
    blob = gcs().bucket(BUCKET).blob(key)
    return blob.create_resumable_upload_session(size=size, content_type=ctype or "application/octet-stream", timeout=60)


def _ctype(path: str) -> str:
    return {"json": "application/json", "jsonl": "application/x-ndjson", "mov": "video/quicktime", "jpg": "image/jpeg",
            "csv": "text/csv", "ply": "application/octet-stream", "bin": "application/octet-stream"}.get(path.rsplit(".", 1)[-1], "application/octet-stream")


def _check_path(p: str):
    if p.startswith("/") or ".." in p.split("/") or not p:
        raise HTTPException(400, f"bad path {p!r}")


@app.post("/v1/scans")
def create_scan(body: CreateScan, user: str = Depends(require_user)):
    m = body.manifest
    if m.get("schema") != "capture_manifest.v1":
        raise HTTPException(400, "manifest.schema must be capture_manifest.v1")
    files = m.get("files") or []
    if not files:
        raise HTTPException(400, "manifest.files is empty")
    scan_id = new_scan_id()
    m["scan_id"] = scan_id
    raw = f"raw/{scan_id}"
    gcs().bucket(BUCKET).blob(f"{raw}/manifest.json").upload_from_string(json.dumps(m, indent=1), content_type="application/json")
    uploads = []
    for f in files:
        _check_path(f["path"])
        uploads.append({"path": f["path"], "url": _session(f"{raw}/{f['path']}", int(f["size"]), _ctype(f["path"]))})
    db().collection("scans").document(scan_id).set({
        "scan_id": scan_id, "owner": user, "name": m.get("name") or "Scan", "created_at": now_iso(),
        "captured_at": m.get("created_at"), "status": "uploading", "stage": None, "stages": {}, "versions": [],
        "device": m.get("device"), "upload": {"files": len(files), "bytes": sum(int(f["size"]) for f in files)},
    })
    return {"scan_id": scan_id, "uploads": uploads}


@app.post("/v1/scans/{scan_id}/uploads")
def refresh_uploads(scan_id: str, body: Paths, user: str = Depends(require_user)):
    scan_doc(scan_id, user)
    m = json.loads(gcs().bucket(BUCKET).blob(f"raw/{scan_id}/manifest.json").download_as_bytes())
    sizes = {f["path"]: int(f["size"]) for f in m["files"]}
    out = []
    for p in body.paths:
        if p not in sizes:
            raise HTTPException(400, f"{p} not in manifest")
        out.append({"path": p, "url": _session(f"raw/{scan_id}/{p}", sizes[p], _ctype(p))})
    return {"uploads": out}


@app.post("/v1/scans/{scan_id}/complete")
def complete(scan_id: str, user: str = Depends(require_user)):
    d = scan_doc(scan_id, user)
    if d.get("status") not in ("uploading", "failed"):
        return {"status": d.get("status")}
    bucket = gcs().bucket(BUCKET)
    m = json.loads(bucket.blob(f"raw/{scan_id}/manifest.json").download_as_bytes())
    problems = []
    for f in m["files"]:
        b = bucket.get_blob(f"raw/{scan_id}/{f['path']}")
        if b is None:
            problems.append({"path": f["path"], "problem": "missing"})
        elif b.size != int(f["size"]) or (f.get("crc32c") and b.crc32c != f["crc32c"]):
            problems.append({"path": f["path"], "problem": "mismatch", "size": b.size, "crc32c": b.crc32c})
    if problems:
        raise HTTPException(409, {"message": "upload incomplete", "files": problems})
    version = max(d.get("versions") or [0]) + 1
    db().collection("scans").document(scan_id).update({"status": "queued", "processing_version": version, "queued_at": now_iso()})
    ex = start_workflow(scan_id, version)
    db().collection("scans").document(scan_id).update({"execution": ex})
    return {"status": "queued", "version": version}


@app.get("/v1/scans")
def list_scans(request: Request, user: str = Depends(require_user)):
    q = db().collection("scans").where("owner", "==", user)
    docs = sorted((s.to_dict() for s in q.stream()), key=lambda d: d.get("created_at") or "", reverse=True)
    return [public_scan(d, request) for d in docs[:200]]


@app.get("/v1/scans/{scan_id}")
def get_scan(scan_id: str, request: Request, user: str = Depends(require_user)):
    return public_scan(scan_doc(scan_id, user), request, full=True)


@app.post("/v1/scans/{scan_id}/reprocess")
def reprocess(scan_id: str, body: Reprocess, user: str = Depends(require_user)):
    d = scan_doc(scan_id, user)
    if body.from_stage not in STAGES:
        raise HTTPException(400, f"from_stage must be one of {STAGES}")
    if d.get("status") in ("queued", "processing"):
        raise HTTPException(409, "scan is already processing")
    version = max((d.get("versions") or []) + [d.get("processing_version") or 0]) + 1
    db().collection("scans").document(scan_id).update({"status": "queued", "processing_version": version})
    ex = start_workflow(scan_id, version, body.from_stage, body.config, body.only)
    return {"version": version, "execution": ex}


# ----------------------------------------------------------------------------- per-stage processing report
_report_cache: dict[tuple[str, int], tuple[float, dict]] = {}
REPORT_IMG_EXT = (".jpg", ".jpeg", ".png", ".svg")


def _blob_text(bucket, key: str, size: int | None = None, max_bytes: int | None = None, tail: bool = False) -> str | None:
    """One GET (no metadata round trip): the listing already told us the size."""
    try:
        b = bucket.blob(key)
        if max_bytes and size and size > max_bytes:
            start = size - max_bytes if tail else 0
            data = b.download_as_bytes(start=start, end=start + max_bytes - 1)
        else:
            data = b.download_as_bytes()
        return data.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        log.exception("report read %s", key)
        return None


def _parse_json(t: str | None):
    try:
        return json.loads(t) if t else None
    except ValueError:
        return None


def build_report(d: dict, version: int, base: str = "") -> dict:
    """Everything we know about one processing version, stage by stage: Firestore stage state + metrics, the
    stage.json each job wrote, its files (aggregated by folder), log tails, charts data and signed image URLs."""
    from concurrent.futures import ThreadPoolExecutor

    scan_id = d["scan_id"]
    bucket = gcs().bucket(BUCKET)
    # one listing of all work outputs for this scan, grouped by (version, stage)
    by_vs: dict[tuple[int, str], list] = {}
    for b in gcs().list_blobs(BUCKET, prefix=f"work/{scan_id}/v"):
        parts = b.name.split("/")
        if len(parts) < 5 or not parts[2][1:].isdigit():
            continue
        by_vs.setdefault((int(parts[2][1:]), parts[3]), []).append((("/".join(parts[4:])), b.size or 0, b.name))
    st = d.get("stages") or {}
    stage_versions = d.get("stage_versions") or {}

    def ver_for(stage: str) -> int | None:
        # the run that produced this version's input: its own dir, else the latest earlier one that completed
        for v in range(version, 0, -1):
            if any(f[0] == "stage.json" for f in by_vs.get((v, stage), [])):
                return v
        return stage_versions.get(stage)

    # images go through /v1/w/<token>/... which signs lazily on load (signing ~80 URLs up front took ~10 s)
    wtok = make_token(scan_id, 6 * 3600, "work")

    def url_of(key: str) -> str:
        return f"{base}/v1/w/{wtok}/{scan_id}/{key.split('/', 2)[2]}"

    # prefetch every small file the report reads, in parallel
    stage_dir = {n: ver_for(n) for n in STAGES}
    wanted: dict[str, tuple[int, int | None, bool]] = {}
    extra = {"ingest": ["qa.json"], "frames": ["frames.json"], "sfm": ["sfm.json"], "depth": ["world.json"],
             "splat": ["curve.json"], "densify": ["views.json"], "plan": ["agent/transcript.json"]}
    for n, sv in stage_dir.items():
        if not sv:
            continue
        for rel, size, key in by_vs.get((sv, n), []):
            if rel == "stage.json" or rel in extra.get(n, []):
                wanted[key] = (size, None, False)
            elif rel.endswith(".log") and "/" not in rel:
                wanted[key] = (size, 24_000, True)
    with ThreadPoolExecutor(16) as ex:
        got = dict(zip(wanted, ex.map(lambda k: _blob_text(bucket, k, wanted[k][0], wanted[k][1], wanted[k][2]), wanted)))

    def _blob_json(_bucket, key: str):
        return _parse_json(got.get(key))

    stages_out = []
    for name in STAGES:
        sv = stage_dir[name]
        files = by_vs.get((sv, name), []) if sv else []
        pre = f"work/{scan_id}/v{sv:02d}/{name}" if sv else None
        x = st.get(name) or {}
        r: dict[str, Any] = {
            "name": name, "version": sv, "reused": bool(sv and sv != version),
            "status": x.get("status") if x.get("version") in (None, sv, version) else "pending",
            **{k: x.get(k) for k in ("started_at", "ended_at", "duration_s", "progress", "note", "metrics")},
        }
        if x.get("status") == "failed" and x.get("traceback"):
            r["error"] = str(x.get("traceback"))[-4000:]
        elif x.get("error"):
            r["error"] = str(x.get("error"))[-4000:]
        if pre and any(f[0] == "stage.json" for f in files):
            sj = _blob_json(bucket, f"{pre}/stage.json") or {}
            r["job"] = {k: sj.get(k) for k in ("config", "image", "git_sha", "duration_s", "finished_at", "started_at")}
            if not r.get("metrics"):
                r["metrics"] = sj.get("metrics")
        # files, aggregated by first-level folder
        agg: dict[str, dict] = {}
        for rel, size, _ in files:
            k = rel.split("/")[0] + ("/" if "/" in rel else "")
            a = agg.setdefault(k, {"path": k, "files": 0, "bytes": 0})
            a["files"] += 1
            a["bytes"] += size
        r["files"] = sorted(agg.values(), key=lambda a: a["path"])
        r["bytes"] = sum(a["bytes"] for a in agg.values())
        r["logs"] = []
        for rel, size, key in files:
            if rel.endswith(".log") and "/" not in rel:
                r["logs"].append({"name": rel, "bytes": size, "tail": got.get(key)})
        imgs: list[tuple[str, str]] = []
        data: dict[str, Any] = {}
        if pre:
            if name == "ingest":
                data["qa"] = _blob_json(bucket, f"{pre}/qa.json")
            elif name == "frames":
                fj = _blob_json(bucket, f"{pre}/frames.json") or {}
                fr = fj.get("frames") or []
                data["frames"] = [{"name": f["name"], "t": round(f.get("t") or 0, 3), "sharp": round(f.get("sharpness") or 0, 1),
                                   "depth": bool(f.get("depth")), "kind": f.get("kind")} for f in fr]
            elif name == "sfm":
                sj = _blob_json(bucket, f"{pre}/sfm.json") or {}
                res = sj.get("arkit_residual_m") or {}
                data["residuals"] = sorted(([k.rsplit("/", 1)[-1].rsplit(".", 1)[0], round(v, 4)] for k, v in res.items()))
            elif name == "depth":
                data["world"] = _blob_json(bucket, f"{pre}/world.json")
            elif name == "splat":
                data["curve"] = _blob_json(bucket, f"{pre}/curve.json")
                imgs += [(rel, key) for rel, _, key in files if rel.startswith("eval/") and rel.endswith(".jpg")]
            elif name == "densify":
                vj = _blob_json(bucket, f"{pre}/views.json")
                data["views"] = (vj or {}).get("views", [])
                imgs += [(rel, key) for rel, _, key in files if rel.startswith("preview/") and rel.endswith(".jpg")]
            elif name == "plan":
                data["transcript"] = _blob_json(bucket, f"{pre}/agent/transcript.json")
                imgs += [(rel, key) for rel, _, key in files if "/" not in rel and rel.endswith(REPORT_IMG_EXT)]
                imgs += [(rel, key) for rel, _, key in files if rel.startswith("agent/") and rel.endswith(REPORT_IMG_EXT)]
            elif name == "publish":
                imgs += [(rel, key) for rel, _, key in files if rel == "thumb.jpg"]
                thumbs = sorted(key for rel, _, key in files if rel.startswith("photos/thumb/"))
                if thumbs:
                    step = max(1, len(thumbs) // 24)
                    data["photo_thumbs"] = [{"name": k.rsplit("/", 1)[-1], "url": url_of(k)} for k in thumbs[::step][:24]]
        r["data"] = data
        r["images"] = [{"name": rel, "url": url_of(key)} for rel, key in imgs]
        stages_out.append(r)
    return {"scan_id": scan_id, "name": d.get("name"), "version": version, "status": d.get("status"),
            "versions": d.get("versions") or [], "processing_version": d.get("processing_version"),
            "current_version": d.get("current_version"), "created_at": d.get("created_at"),
            "qa": d.get("qa"), "upload": d.get("upload"), "stages": stages_out, "generated_at": now_iso()}


@app.get("/v1/scans/{scan_id}/report")
def get_report(scan_id: str, request: Request, v: int | None = None, user: str = Depends(require_user)):
    d = scan_doc(scan_id, user)
    d.setdefault("scan_id", scan_id)
    version = v or d.get("processing_version") or d.get("current_version") or 1
    busy = d.get("status") in ("queued", "processing", "uploading")
    hit = _report_cache.get((scan_id, version))
    if hit and time.time() - hit[0] < (20 if busy else 300):
        return JSONResponse(hit[1], headers={"Cache-Control": "no-store"})
    rep = build_report(d, version, _abs(request))
    _report_cache[(scan_id, version)] = (time.time(), rep)
    return JSONResponse(rep, headers={"Cache-Control": "no-store"})


def _authorize_view(scan_id: str, authorization: str | None, share: str | None) -> dict:
    who = owner_from_token(authorization)
    if who:
        return scan_doc(scan_id, who)
    if share and check_token(share, scan_id, kinds=("share",)):
        return scan_doc(scan_id)
    if AUTH_DISABLED:
        return scan_doc(scan_id)
    raise HTTPException(401, "bearer token or share token required")


def _abs(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    if request.headers.get("x-forwarded-proto") == "https":
        base = base.replace("http://", "https://", 1)
    return base


@app.get("/v1/scans/{scan_id}/scene")
def get_scene(scan_id: str, request: Request, v: int | None = None, share: str | None = None,
              authorization: str | None = Header(default=None)):
    d = _authorize_view(scan_id, authorization, share)
    ver = v or d.get("current_version")
    if not ver:
        raise HTTPException(404, "scan has no published version yet")
    try:
        scene = json.loads(gcs().bucket(BUCKET).blob(f"processed/{scan_id}/v{int(ver):02d}/scene.json").download_as_bytes())
    except Exception:  # noqa: BLE001
        raise HTTPException(404, f"version {ver} not found")
    tok = make_token(scan_id, ASSET_TTL, "asset")
    base = f"{_abs(request)}/v1/f/{tok}/{scan_id}/v{int(ver):02d}/"

    def fix(u):
        return base + u if u and not u.startswith("http") else u

    for layer in scene.get("layers", []):
        for k in ("url", "fallback_url"):
            if layer.get(k):
                layer[k] = fix(layer[k])
        for s in layer.get("styles") or []:
            s["url"] = fix(s["url"])
    if scene.get("building_model_url"):
        scene["building_model_url"] = fix(scene["building_model_url"])
    scene["base_url"] = base
    scene["versions"] = d.get("versions") or [ver]
    return JSONResponse(scene, headers={"Cache-Control": "no-store"})


@app.api_route("/v1/w/{tok}/{scan_id}/{path:path}", methods=["GET", "HEAD"])
def work_file(tok: str, scan_id: str, path: str):
    """Pipeline work outputs referenced by the processing report (eval renders, plan images, thumbnails)."""
    if not check_token(tok, scan_id, kinds=("work",)):
        raise HTTPException(403, "invalid or expired token")
    _check_path(path)
    if not path.split("/", 1)[0][1:].isdigit() or not path.startswith("v"):
        raise HTTPException(400, "bad path")
    return RedirectResponse(sign_get(f"work/{scan_id}/{path}", 3600), status_code=302,
                            headers={"Cache-Control": "private, max-age=1800"})


@app.api_route("/v1/f/{tok}/{scan_id}/{ver}/{path:path}", methods=["GET", "HEAD"])
def asset(tok: str, scan_id: str, ver: str, path: str):
    if not check_token(tok, scan_id):
        raise HTTPException(403, "invalid or expired asset token")
    if not ver.startswith("v") or not ver[1:].isdigit():
        raise HTTPException(400, "bad version")
    _check_path(path)
    return RedirectResponse(sign_get(f"processed/{scan_id}/{ver}/{path}", 3600), status_code=302,
                            headers={"Cache-Control": "private, max-age=1800"})


@app.post("/v1/scans/{scan_id}/share")
def share(scan_id: str, request: Request, user: str = Depends(require_user)):
    d = scan_doc(scan_id, user)
    tok = make_token(scan_id, None, "share")
    from google.cloud import firestore

    db().collection("scans").document(scan_id).update({"share_tokens": firestore.ArrayUnion([tok])})
    v = d.get("current_version")
    return {"share_url": f"{_abs(request)}/scans/{scan_id}?share={tok}" + (f"&v={v}" if v else ""), "token": tok}


@app.delete("/v1/scans/{scan_id}/share")
def unshare(scan_id: str, user: str = Depends(require_user)):
    scan_doc(scan_id, user)
    db().collection("scans").document(scan_id).update({"share_tokens": []})
    return {"ok": True}


@app.post("/v1/devices/push-token", status_code=204)
def push_token(body: PushToken, user: str = Depends(require_user)):
    key = hashlib.sha1(body.expo_push_token.encode()).hexdigest()
    db().collection("devices").document(key).set({"owner": user, "token": body.expo_push_token, "updated_at": now_iso()})


# ----------------------------------------------------------------------------- viewer SPA
@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith("v1/"):
        raise HTTPException(404)
    f = (VIEWER_DIR / path).resolve()
    if path and f.is_file() and VIEWER_DIR.resolve() in f.parents:
        cache = "public, max-age=31536000, immutable" if "/assets/" in f"/{path}" else "no-cache"
        return FileResponse(f, headers={"Cache-Control": cache})
    idx = VIEWER_DIR / "index.html"
    if idx.exists():
        return FileResponse(idx, headers={"Cache-Control": "no-cache"})
    return JSONResponse({"service": "scanner-api", "viewer": "not bundled"})
