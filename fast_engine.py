import os
import re
import json
import shutil
import subprocess
from typing import List, Dict, Any, Generator

# Find ripgrep binary
RG_BIN = shutil.which("rg")
if not RG_BIN:
    candidate = os.path.expanduser(r"~\scoop\shims\rg.exe")
    if os.path.exists(candidate):
        RG_BIN = candidate

VALID_EXTS = ['.txt', '.csv', '.sql', '.log', '.tsv', '.json', '.xml', '.dat', '.ini', '.cfg', '.yaml', '.yml', '.env', '.conf', '.bak', '.dump']

def search_files_generator(root_dir: str, query: str, mode: str = "OR", is_regex: bool = False, max_matches: int = 100000) -> Generator[Dict[str, Any], None, None]:
    if not query.strip():
        yield {"type": "error", "message": "Query cannot be empty"}
        return

    # Count total files quickly
    total_files = 0
    for _, _, filenames in os.walk(root_dir):
        for f in filenames:
            _, ext = os.path.splitext(f)
            if ext.lower() in VALID_EXTS or not ext:
                total_files += 1

    yield {
        "type": "init",
        "total_files": total_files
    }

    # If Ripgrep is available, use Rust-powered lightning search!
    if RG_BIN and os.path.exists(RG_BIN):
        yield from _search_with_ripgrep(root_dir, query, mode, is_regex, max_matches, total_files)
    else:
        yield from _search_with_python(root_dir, query, mode, is_regex, max_matches, total_files)

def _search_with_ripgrep(root_dir: str, query: str, mode: str, is_regex: bool, max_matches: int, total_files: int):
    # Prepare regex pattern
    pattern = ""
    if is_regex:
        pattern = query
    else:
        parts = re.split(r'[|,،\n]+', query)
        escaped_parts = [re.escape(p.strip()) for p in parts if p.strip()]
        if not escaped_parts:
            yield {"type": "error", "message": "No search terms provided"}
            return
        
        if mode == "AND":
            # Regex lookahead for AND: ^(?=.*term1)(?=.*term2)
            pattern = "^" + "".join(f"(?=.*{p})" for p in escaped_parts) + ".*$"
        else: # OR
            pattern = "|".join(escaped_parts)

    import multiprocessing
    cpu_count = multiprocessing.cpu_count()

    cmd = [
        RG_BIN,
        "--json",
        "--line-buffered",     # force immediate line output without pipe buffering
        "-i",                  # case insensitive
        "--no-messages",       # suppress errors
        "--mmap",              # memory-mapped I/O for large files
        "--threads", str(cpu_count),  # use all CPU cores
        "-e", pattern,
        root_dir
    ]

    # Use PCRE2 only if lookaheads are required (e.g. AND mode)
    if mode == "AND" and not is_regex:
        cmd.extend(["--pcre2"])

    # Include all text formats via glob (keep .txt.txt type files too)
    for ext in VALID_EXTS:
        cmd.extend(["-g", f"*{ext}"])
    # Also include files with double extensions like .txt.txt
    cmd.extend(["-g", "*.txt.*", "-g", "*.csv.*"])

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1
        )

        batch = []
        total_matches = 0
        scanned_files_set = set()
        scanned_files_count = 0
        estimated_lines = 0

        for line in proc.stdout:
            try:
                data = json.loads(line)
                msg_type = data.get("type")

                if msg_type == "begin" or msg_type == "end":
                    if msg_type == "end":
                        scanned_files_count += 1
                        estimated_lines += 250000
                    # Send live progress on every file scanned
                    yield {
                        "type": "progress",
                        "scanned_files": max(scanned_files_count, len(scanned_files_set)),
                        "total_files": total_files,
                        "scanned_lines": estimated_lines,
                        "total_matches": total_matches,
                        "matches": batch
                    }
                    batch = []

                elif msg_type == "match":
                    payload = data["data"]
                    path_text = payload["path"]["text"]
                    line_num = payload["line_number"]
                    line_text = payload["lines"]["text"].strip()

                    scanned_files_set.add(path_text)
                    rel_d = os.path.relpath(os.path.dirname(path_text), root_dir)
                    if rel_d == ".": rel_d = "(Root)"
                    filename = os.path.basename(path_text)

                    total_matches += 1
                    batch.append({
                        "folder": rel_d,
                        "file": filename,
                        "line": line_num,
                        "content": line_text[:1000]
                    })

                    # Stream matches immediately to UI without waiting!
                    yield {
                        "type": "progress",
                        "scanned_files": max(scanned_files_count, len(scanned_files_set)),
                        "total_files": total_files,
                        "scanned_lines": max(estimated_lines, total_matches * 10),
                        "total_matches": total_matches,
                        "matches": batch
                    }
                    batch = []

                    if total_matches >= max_matches:
                        break

            except Exception:
                continue

        proc.terminate()

        yield {
            "type": "complete",
            "scanned_files": total_files,
            "total_files": total_files,
            "scanned_lines": max(estimated_lines, total_matches * 10),
            "total_matches": total_matches,
            "matches": batch
        }

    except Exception as e:
        yield from _search_with_python(root_dir, query, mode, is_regex, max_matches, total_files)


def _search_with_python(root_dir: str, query: str, mode: str, is_regex: bool, max_matches: int, total_files: int):
    # Fallback pure python binary search
    terms = [re.escape(p.strip().lower()).encode() for p in re.split(r'[|,،\n]+', query) if p.strip()]
    scanned_files = 0
    scanned_lines = 0
    total_matches = 0
    batch = []

    for dirpath, _, filenames in os.walk(root_dir):
        for f in filenames:
            _, ext = os.path.splitext(f)
            if ext.lower() in VALID_EXTS:
                scanned_files += 1
                fp = os.path.join(dirpath, f)
                rel_d = os.path.relpath(dirpath, root_dir)
                if rel_d == ".": rel_d = "(Root)"

                try:
                    with open(fp, 'rb') as fl:
                        for l_idx, raw_line in enumerate(fl, 1):
                            scanned_lines += 1
                            raw_low = raw_line.lower()
                            if mode == "AND":
                                m = all(t in raw_low for t in terms)
                            else:
                                m = any(t in raw_low for t in terms)

                            if m:
                                total_matches += 1
                                batch.append({
                                    "folder": rel_d,
                                    "file": f,
                                    "line": l_idx,
                                    "content": raw_line.decode('utf-8', errors='replace').strip()[:1000]
                                })
                                if len(batch) >= 20:
                                    yield {
                                        "type": "progress",
                                        "scanned_files": scanned_files,
                                        "total_files": total_files,
                                        "scanned_lines": scanned_lines,
                                        "total_matches": total_matches,
                                        "matches": batch
                                    }
                                    batch = []
                                if total_matches >= max_matches: break
                except Exception:
                    pass

                if total_matches >= max_matches: break
        if total_matches >= max_matches: break

    yield {
        "type": "complete",
        "scanned_files": scanned_files,
        "total_files": total_files,
        "scanned_lines": scanned_lines,
        "total_matches": total_matches,
        "matches": batch
    }