import json
import os
import time

os.environ["SECRET_SHARE_SIGNING_KEY"] = "k" * 40
os.environ["SECRET_API_TOKENS"] = json.dumps({"tok-123": "a@b.c"})

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402


def test_asset_token_roundtrip():
    t = main.make_token("scan_x", 60, "asset")
    assert main.check_token(t, "scan_x")
    assert not main.check_token(t, "scan_y")
    assert not main.check_token(t + "x", "scan_x")
    raw, sig = t.split(".")
    assert not main.check_token(raw[:-2] + "AA." + sig, "scan_x")


def test_asset_token_expiry(monkeypatch):
    t = main.make_token("scan_x", 1, "asset")
    real = time.time
    monkeypatch.setattr(main.time, "time", lambda: real() + 5)
    assert not main.check_token(t, "scan_x")


def test_asset_token_cannot_be_share():
    t = main.make_token("scan_x", 60, "asset")
    assert not main.check_token(t, "scan_x", kinds=("share",))


def test_bearer_auth():
    assert main.owner_from_token("Bearer tok-123") == "a@b.c"
    assert main.owner_from_token("Bearer nope") is None
    c = TestClient(main.app)
    assert c.get("/v1/scans", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/v1/scans").status_code == 401


def test_asset_requires_valid_token():
    c = TestClient(main.app)
    r = c.get("/v1/f/garbage/scan_x/v01/scene.json", follow_redirects=False)
    assert r.status_code == 403


def test_path_traversal_rejected(monkeypatch):
    monkeypatch.setattr(main, "sign_get", lambda key, ttl=0: "https://signed/" + key)
    c = TestClient(main.app)
    t = main.make_token("scan_x", 60, "asset")
    assert c.get(f"/v1/f/{t}/scan_x/v01/../../raw/x", follow_redirects=False).status_code in (400, 404)
    r = c.get(f"/v1/f/{t}/scan_x/v01/photos/a.jpg", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://signed/processed/scan_x/v01/photos/a.jpg"
