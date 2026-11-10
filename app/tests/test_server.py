"""API round trip against a temporary library (real conversion subprocess)."""

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "1706.03762.pdf"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    if not SAMPLE.exists():
        pytest.skip("sample PDF missing")
    monkeypatch.setattr(config, "PAPERS_DIR", tmp_path / "papers")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "papers" / "inbox")
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path / "papers" / "library")
    monkeypatch.setattr(config, "TMP_DIR", tmp_path / "papers" / ".tmp")
    monkeypatch.setattr(config, "WEB_DIR", tmp_path / "web-missing")
    from backend import server

    server.watcher.inbox = config.INBOX_DIR
    with TestClient(server.app, base_url=f"http://127.0.0.1:{server.config.PORT}") as c:
        yield c


def _wait_ready(client, pid, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = next(p for p in client.get("/api/papers").json() if p["id"] == pid)
        if s["status"] != "processing":
            return s
        time.sleep(0.5)
    raise AssertionError("conversion did not finish")


def test_upload_read_progress_delete(client):
    assert client.get("/api/papers").json() == []
    # web/ missing -> placeholder page, not a crash
    r = client.get("/")
    assert r.status_code == 200 and "Reading tool" in r.text

    with open(SAMPLE, "rb") as f:
        r = client.post("/api/papers", files={"file": ("my  paper (v2).pdf", f, "application/pdf")})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["status"] == "processing" and s["source_filename"] == "my  paper (v2).pdf"
    pid = s["id"]
    assert pid.startswith("vaswani-2017-attention-is-all-you-need-")
    assert client.get(f"/api/papers/{pid}").status_code == 404  # not ready yet

    s = _wait_ready(client, pid)
    assert s["status"] == "ready", s
    assert s["num_figures"] >= 2 and s["num_tables"] == 4 and s["word_count"] > 2000

    paper = client.get(f"/api/papers/{pid}").json()
    assert paper["blocks"] and paper["sections"] and paper["assets"]
    asset = paper["assets"][0]
    r = client.get(f"/api/papers/{pid}/assets/{asset['image']}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert client.get(f"/api/papers/{pid}/assets/..%2Fsummary.json").status_code == 404
    r = client.get(f"/api/papers/{pid}/pdf")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    assert "inline" in r.headers["content-disposition"]

    # duplicate upload -> same id, no second entry
    with open(SAMPLE, "rb") as f:
        r = client.post("/api/papers", files={"file": ("copy.pdf", f, "application/pdf")})
    assert r.json()["id"] == pid and len(client.get("/api/papers").json()) == 1

    # progress
    p = client.get(f"/api/papers/{pid}/progress").json()
    assert p["block"] == 0 and p["wpm"] == 350
    body = {"block": 12, "word": 3, "wpm": 420, "percent": 37.5, "updated": "2026-11-10T12:00:00Z"}
    assert client.put(f"/api/papers/{pid}/progress", json=body).json() == body
    assert client.get(f"/api/papers/{pid}/progress").json()["percent"] == 37.5
    assert client.get("/api/papers").json()[0]["progress"]["block"] == 12

    # errors keep the {"detail": "<string>"} shape, also for validation errors
    r = client.put(f"/api/papers/{pid}/progress", json={"block": "x"})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    assert client.get("/api/papers/nope").status_code == 404
    assert client.get("/api/papers/nope").json()["detail"]
    r = client.post("/api/papers", files={"file": ("x.pdf", b"hello", "application/pdf")})
    assert r.status_code == 400

    # reingest
    r = client.post(f"/api/papers/{pid}/reingest")
    assert r.status_code == 200 and r.json()["status"] == "processing"
    assert _wait_ready(client, pid)["status"] == "ready"
    # progress survives a reingest
    assert client.get(f"/api/papers/{pid}/progress").json()["block"] == 12

    assert client.delete(f"/api/papers/{pid}").status_code == 204
    assert client.get("/api/papers").json() == []
    assert not (config.LIBRARY_DIR / pid).exists()


def test_inbox_picks_up_pdf(client):
    dest = config.INBOX_DIR / "dropped  file.pdf"
    shutil.copyfile(SAMPLE, dest)
    t0 = time.time()
    while time.time() - t0 < 30 and dest.exists():
        time.sleep(0.3)
    assert not dest.exists(), "inbox file should have been moved into the library"
    papers = client.get("/api/papers").json()
    assert len(papers) == 1 and papers[0]["source_filename"] == "dropped  file.pdf"
    pid = papers[0]["id"]
    assert (config.LIBRARY_DIR / pid / "original.pdf").exists()
    assert _wait_ready(client, pid)["status"] == "ready"
    # dropping the same bytes again does not create a duplicate
    shutil.copyfile(SAMPLE, config.INBOX_DIR / "again.pdf")
    t0 = time.time()
    while time.time() - t0 < 30 and (config.INBOX_DIR / "again.pdf").exists():
        time.sleep(0.3)
    assert len(client.get("/api/papers").json()) == 1


def test_requests_from_other_sites_are_refused(client):
    # DNS rebinding: a foreign hostname pointed at 127.0.0.1
    assert client.get("/api/papers", headers={"host": "evil.example:8377"}).status_code == 400
    # cross-site form upload / re-conversion from a web page
    bad = {"origin": "https://evil.example"}
    assert client.post("/api/papers", files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")}, headers=bad).status_code == 403
    assert client.post("/api/papers/some-id/reingest", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    # the app's own pages still work
    assert client.get("/api/papers", headers={"origin": f"http://127.0.0.1:{client.base_url.port}"}).status_code == 200
