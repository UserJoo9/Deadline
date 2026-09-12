import os
import json
import webbrowser
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
import uvicorn
from fast_engine import search_files_generator

app = FastAPI(title="DEADLINE Search Platform")
ROOT_DIR = os.path.abspath(".")

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

@app.get("/api/search")
async def search_endpoint(q: str, mode: str = "OR", is_regex: bool = False, folder: str = ""):
    def event_stream():
        scan_dir = ROOT_DIR
        if folder and folder.strip():
            f_clean = folder.strip()
            if os.path.isabs(f_clean) and os.path.exists(f_clean):
                scan_dir = f_clean
            else:
                candidate = os.path.normpath(os.path.join(ROOT_DIR, f_clean))
                if os.path.exists(candidate):
                    scan_dir = candidate

        for event in search_files_generator(scan_dir, query=q, mode=mode, is_regex=is_regex):
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(event_stream(), media_type="text/plain")

if __name__ == "__main__":
    print(f"[*] DEADLINE Search Platform")
    print(f"[*] Engine  : Ripgrep")
    print(f"[*] Root dir: {ROOT_DIR}")
    print(f"[*] URL     : http://127.0.0.1:8000")
    webbrowser.open("http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
