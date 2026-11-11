"""FastAPI app: JSON API for the library + static serving of web/.

Run:  uv run python -m backend.server        (http://127.0.0.1:8377)
"""

from __future__ import annotations

import logging
import mimetypes
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from . import annotate, config, library
from .inbox import InboxWatcher
from .worker import IngestWorker

log = logging.getLogger("server")

worker = IngestWorker()
watcher = InboxWatcher(on_added=worker.enqueue)

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("image/svg+xml", ".svg")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    worker.start()
    # papers left half-converted by a previous run get another go
    for s in library.list_summaries():
        if s.get("status") == "processing":
            worker.enqueue(s["id"])
    watcher.start()
    log.info("library: %s | inbox: %s", config.LIBRARY_DIR, config.INBOX_DIR)
    try:
        yield
    finally:
        watcher.stop()
        worker.stop()


app = FastAPI(title="Saccade", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Local-only guard. The server has no login because it only listens on 127.0.0.1,
# but a web page open in the same browser can still send requests to that address.
# Two checks keep other sites out:
#  - Host must be this machine's own address (stops DNS-rebinding, where a hostile
#    domain is pointed at 127.0.0.1 to read the library through the browser);
#  - requests that change something must come from the app's own pages, not from
#    another site (stops cross-site uploads / re-conversions).
# ---------------------------------------------------------------------------

_LOCAL_NAMES = ("127.0.0.1", "localhost", "[::1]")
ALLOWED_HOSTS = {f"{h}:{config.PORT}" for h in _LOCAL_NAMES}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = (request.headers.get("host") or "").lower()
    if host not in ALLOWED_HOSTS:
        return JSONResponse(status_code=400, content={"detail": "This server only answers on its local address"})
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        site = request.headers.get("sec-fetch-site")
        if (origin is not None and origin.lower() not in ALLOWED_ORIGINS) or site in ("cross-site", "same-site"):
            return JSONResponse(status_code=403, content={"detail": "Requests from other sites are not accepted"})
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


@app.exception_handler(RequestValidationError)
async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """Keep the contract's error shape: {"detail": "<message>"}."""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()) if x != "body")
        parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return JSONResponse(status_code=422, content={"detail": "; ".join(parts) or "Invalid request"})


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _summary_or_404(pid: str) -> dict:
    s = library.get_summary(pid)
    if s is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    return s


class ProgressIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    block: int = 0
    word: int = 0
    wpm: int = 350
    percent: float = 0.0
    updated: str | None = None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/papers")
def list_papers() -> list[dict]:
    return [library.public_summary(s) for s in library.list_summaries()]


@app.post("/api/papers")
def upload_paper(file: UploadFile = File(...)) -> dict:
    name = library.safe_filename(file.filename or "upload.pdf")
    tmp = library.tmp_upload_path(name)
    try:
        with open(tmp, "wb") as f:
            shutil.copyfileobj(file.file, f, length=1 << 20)
        with open(tmp, "rb") as f:
            head = f.read(1024)
        if b"%PDF" not in head:
            raise HTTPException(status_code=400, detail="That file is not a PDF")
        summary, created = library.add_pdf(tmp, name, move=True)
        if created or summary.get("status") == "error":
            if not created:
                library.set_status(summary["id"], "processing")
            worker.enqueue(summary["id"])
            summary = library.get_summary(summary["id"]) or summary
        return library.public_summary(summary)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


@app.get("/api/papers/{pid}")
def get_paper(pid: str):
    s = _summary_or_404(pid)
    if s.get("status") != "ready":
        if s.get("status") == "error":
            raise HTTPException(status_code=404, detail=f"Conversion failed: {s.get('error')}")
        raise HTTPException(status_code=404, detail="Paper is still being converted")
    paper = library.read_json(config.LIBRARY_DIR / pid / "paper.json")
    if not isinstance(paper, dict):
        raise HTTPException(status_code=404, detail="Paper data missing; try reingest")
    pub = library.public_summary(s)
    paper.update(pub)
    return JSONResponse(paper)


@app.delete("/api/papers/{pid}", status_code=204)
def delete_paper(pid: str) -> Response:
    _summary_or_404(pid)
    worker.cancel(pid)
    library.delete_paper(pid)
    return Response(status_code=204)


@app.get("/api/papers/{pid}/progress")
def get_progress(pid: str) -> dict:
    _summary_or_404(pid)
    return library.get_progress(pid)


@app.put("/api/papers/{pid}/progress")
def put_progress(pid: str, body: ProgressIn) -> dict:
    _summary_or_404(pid)
    return library.put_progress(pid, body.model_dump())


@app.get("/api/papers/{pid}/pdf")
def get_pdf(pid: str):
    s = _summary_or_404(pid)
    path = annotate.pdf_path(pid)   # the copy with highlights and notes, when there are any
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF missing")
    return FileResponse(path, media_type="application/pdf", filename=s.get("source_filename") or "paper.pdf",
                        content_disposition_type="inline", headers={"Cache-Control": "no-cache"})


class AnnotationIn(BaseModel):
    block: int
    start: int
    end: int
    text: str
    page: int | None = None
    note: str = ""


class NoteIn(BaseModel):
    note: str = ""


@app.get("/api/papers/{pid}/annotations")
def list_annotations(pid: str) -> list[dict]:
    _summary_or_404(pid)
    return annotate.list_annotations(pid)


@app.post("/api/papers/{pid}/annotations")
def create_annotation(pid: str, body: AnnotationIn) -> dict:
    _summary_or_404(pid)
    if not body.text.strip() or body.end < body.start:
        raise HTTPException(status_code=400, detail="Nothing to highlight")
    return annotate.create(pid, body.block, body.start, body.end, body.text.strip(), body.page, body.note.strip())


@app.put("/api/papers/{pid}/annotations/{aid}")
def update_annotation(pid: str, aid: str, body: NoteIn) -> dict:
    _summary_or_404(pid)
    a = annotate.update(pid, aid, body.note.strip())
    if a is None:
        raise HTTPException(status_code=404, detail="No such highlight")
    return a


@app.delete("/api/papers/{pid}/annotations/{aid}", status_code=204)
def delete_annotation(pid: str, aid: str) -> Response:
    _summary_or_404(pid)
    if not annotate.delete(pid, aid):
        raise HTTPException(status_code=404, detail="No such highlight")
    return Response(status_code=204)


_ASSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.png$")


@app.get("/api/papers/{pid}/assets/{fname}")
def get_asset(pid: str, fname: str):
    _summary_or_404(pid)
    if not _ASSET_RE.match(fname):
        raise HTTPException(status_code=404, detail="No such asset")
    path = config.LIBRARY_DIR / pid / "assets" / fname
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No such asset")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})


@app.post("/api/papers/{pid}/reingest")
def reingest(pid: str) -> dict:
    _summary_or_404(pid)
    s = library.set_status(pid, "processing")
    worker.enqueue(pid)
    return library.public_summary(s)


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def api_404(rest: str):
    raise HTTPException(status_code=404, detail="Not found")


# ---------------------------------------------------------------------------
# static frontend (web/ may be empty or missing while it is being built)
# ---------------------------------------------------------------------------

PLACEHOLDER = """<!doctype html><html><head><meta charset="utf-8"><title>Saccade</title>
<style>body{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem;color:#222}
code{background:#f2f2f2;padding:.1rem .3rem;border-radius:4px}</style></head><body>
<h1>Saccade</h1><p>The server is running, but the web app (<code>web/index.html</code>) is not there yet.</p>
<p>The API is live at <a href="/api/papers">/api/papers</a>. Drop PDFs into <code>papers/inbox/</code> to add them.</p>
</body></html>"""


def _static(rel: str):
    web = config.WEB_DIR.resolve()
    target = (web / rel).resolve() if rel else web
    try:
        target.relative_to(web)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    if target.is_dir():
        target = target / "index.html"
    if target.is_file():
        mt, _ = mimetypes.guess_type(target.name)
        return FileResponse(target, media_type=mt or "application/octet-stream", headers={"Cache-Control": "no-cache"})
    if not rel or rel == "index.html":
        return HTMLResponse(PLACEHOLDER)
    raise HTTPException(status_code=404, detail="Not found")


@app.get("/", include_in_schema=False)
def index():
    return _static("")


@app.get("/{rel:path}", include_in_schema=False)
def static_files(rel: str):
    return _static(rel)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config.ensure_dirs()
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    main()
