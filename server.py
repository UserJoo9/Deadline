import os
import json
import webbrowser
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
import uvicorn
import index_engine

app = FastAPI(title="DEADLINE Search Platform")
ROOT_DIR = os.path.abspath(".")


def _resolve_dir(folder: str) -> str:
    scan_dir = ROOT_DIR
    if folder and folder.strip():
        f_clean = folder.strip()
        if os.path.isabs(f_clean) and os.path.exists(f_clean):
            scan_dir = f_clean
        else:
            candidate = os.path.normpath(os.path.join(ROOT_DIR, f_clean))
            if os.path.exists(candidate):
                scan_dir = candidate
    return scan_dir


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    with open("index.html", "r", encoding="utf-8") as f:
        content = f.read()
    return content

@app.get("/api/info")
async def get_info():
    return {
        "root_dir": ROOT_DIR,
        "engine": "ripgrep"
    }

@app.get("/api/index/status")
async def index_status_endpoint(folder: str = ""):
    return index_engine.index_status(_resolve_dir(folder))

@app.get("/api/index/build")
async def index_build_endpoint(folder: str = "", force: bool = False):
    scan_dir = _resolve_dir(folder)

    def event_stream():
        for event in index_engine.build_index_generator(scan_dir, force=force):
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(event_stream(), media_type="text/plain")

@app.get("/api/search")
async def search_endpoint(q: str, mode: str = "OR", is_regex: bool = False, folder: str = ""):
    scan_dir = _resolve_dir(folder)

    def event_stream():
        # Transparently uses the SQLite trigram index when one exists for
        # this folder, and falls back to the direct ripgrep scan otherwise
        # (including automatically for regex queries and very short terms,
        # which a trigram index can't safely narrow).
        for event in index_engine.search_index_generator(scan_dir, query=q, mode=mode, is_regex=is_regex):
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(event_stream(), media_type="text/plain")

if __name__ == "__main__":
    print(f"[*] DEADLINE Search Platform")
    print(f"[*] Engine  : ripgrep + SQLite FTS5 (trigram) index")
    print(f"[*] Root dir: {ROOT_DIR}")
    print(f"[*] URL     : http://127.0.0.1:8000")
    webbrowser.open("http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
