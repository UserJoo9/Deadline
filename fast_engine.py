import os
import re
import json
import shutil
import subprocess
import threading
import time
from typing import List, Dict, Any, Generator

# Find ripgrep binary
RG_BIN = shutil.which("rg")
if not RG_BIN:
    candidate = os.path.expanduser(r"~\scoop\shims\rg.exe")
    if os.path.exists(candidate):
        RG_BIN = candidate

VALID_EXTS = ['.txt', '.csv', '.sql', '.log', '.tsv', '.json', '.xml', '.dat', '.ini', '.cfg', '.yaml', '.yml', '.env', '.conf', '.bak', '.dump']

# One -g flag per extension. (An earlier version of this file combined them
# into a single brace-alternation glob like "*.{txt,csv,...}" - that relies
# on brace expansion support that isn't guaranteed across every ripgrep
# build/version a user might have installed, and when unsupported it fails
# SILENTLY: rg just never matches any file, so every search returns zero
# results with no error. Correctness beats the minor matcher-count saving.)
_EXT_GLOBS = [f"*{ext}" for ext in VALID_EXTS]

# Remembers the last known file count per root so re-searching the same
# multi-TB folder doesn't pay for a fresh metadata walk before it can even
# start. Walking directory metadata on a spinning disk is itself a lot of
# random seeks across potentially millions of tiny dir entries - on HDD this
# can take longer than the actual grep.
_COUNT_CACHE: Dict[str, int] = {}


def _count_files_async(root_dir: str, result: Dict[str, Any], stop_event: threading.Event) -> None:
    """Counts matching files in the background. Never blocks the search:
    it only feeds the progress bar's denominator, so it's fine if it lags
    behind or finishes after the search itself is done."""
    count = 0
    try:
        for dirpath, _, filenames in os.walk(root_dir):
            if stop_event.is_set():
                return
            for f in filenames:
                _, ext = os.path.splitext(f)
                if ext.lower() in VALID_EXTS:
                    count += 1
            result["total_files"] = count
    except Exception:
        pass
    _COUNT_CACHE[root_dir] = count
    result["total_files"] = count
    result["done"] = True


def search_files_generator(root_dir: str, query: str, mode: str = "OR", is_regex: bool = False, max_matches: int = 100000) -> Generator[Dict[str, Any], None, None]:
    if not query.strip():
        yield {"type": "error", "message": "Query cannot be empty"}
        return

    # Start counting in the background instead of blocking the search behind
    # a full upfront os.walk - on a multi-TB HDD that walk alone can take
    # longer than the grep. Seed it with the last known count so the UI has
    # an immediate, usually-close-enough number from the first frame.
    count_result: Dict[str, Any] = {"total_files": _COUNT_CACHE.get(root_dir, 0), "done": False}
    stop_event = threading.Event()
    counter_thread = threading.Thread(
        target=_count_files_async, args=(root_dir, count_result, stop_event), daemon=True
    )
    counter_thread.start()

    yield {
        "type": "init",
        "total_files": count_result["total_files"]
    }

    try:
        # If Ripgrep is available, use Rust-powered lightning search!
        if RG_BIN and os.path.exists(RG_BIN):
            yield from _search_with_ripgrep(root_dir, query, mode, is_regex, max_matches, count_result)
        else:
            yield from _search_with_python(root_dir, query, mode, is_regex, max_matches, count_result)
    finally:
        stop_event.set()

def build_pattern(query: str, mode: str, is_regex: bool):
    """Turns the user's query into a (pattern, needs_pcre2) pair, or
    (None, error_message) on bad input. Shared by the directory-scan path
    and the index-narrowed path so both match the exact same semantics."""
    if is_regex:
        return query, False

    parts = re.split(r'[|,،\n]+', query)
    escaped_parts = [re.escape(p.strip()) for p in parts if p.strip()]
    if not escaped_parts:
        return None, "No search terms provided"

    if mode == "AND":
        # Regex lookahead for AND: ^(?=.*term1)(?=.*term2)
        pattern = "^" + "".join(f"(?=.*{p})" for p in escaped_parts) + ".*$"
        return pattern, True
    else:  # OR
        return "|".join(escaped_parts), False


def _search_with_ripgrep(root_dir: str, query: str, mode: str, is_regex: bool, max_matches: int, count_result: Dict[str, Any]):
    pattern, needs_pcre2_or_err = build_pattern(query, mode, is_regex)
    if pattern is None:
        yield {"type": "error", "message": needs_pcre2_or_err}
        return
    needs_pcre2 = bool(needs_pcre2_or_err) and mode == "AND" and not is_regex

    yield from run_ripgrep_on_targets(
        targets=[root_dir], pattern=pattern, needs_pcre2=needs_pcre2,
        max_matches=max_matches, count_result=count_result, root_dir=root_dir,
        use_glob=True,
        fallback=lambda: _search_with_python(root_dir, query, mode, is_regex, max_matches, count_result)
    )


def run_ripgrep_on_targets(targets: List[str], pattern: str, needs_pcre2: bool, max_matches: int,
                            count_result: Dict[str, Any], root_dir: str, use_glob: bool, fallback=None):
    """Runs ripgrep over an explicit list of targets. `targets` is either a
    single directory (full scan, `use_glob=True` applies the extension
    filters) or a pre-filtered list of specific file paths (the index
    narrowing path: `use_glob=False`, no directory walk needed at all since
    rg is only ever asked to open files we already know are relevant)."""
    if not targets:
        yield {
            "type": "complete", "scanned_files": 0, "total_files": count_result.get("total_files", 0),
            "scanned_lines": 0, "total_matches": 0, "matches": []
        }
        return

    import multiprocessing
    cpu_count = multiprocessing.cpu_count()
    # On a single spinning HDD, more reader threads usually means MORE time,
    # not less: each thread opens a different file, so the drive head keeps
    # seeking between them instead of streaming one file sequentially. Cap
    # it well below cpu_count by default. If this data lives on an SSD or is
    # spread across several physical disks, raise RG_THREADS accordingly.
    rg_threads = int(os.environ.get("RG_THREADS", min(cpu_count, 4)))

    cmd = [
        RG_BIN,
        "--json",
        "--line-buffered",     # force immediate line output without pipe buffering
        "-i",                  # case insensitive
        "--no-messages",       # suppress errors
        # no --mmap: let rg's own heuristic decide. Forcing mmap over
        # thousands of small files on an HDD causes more page faults/seeks
        # than a plain buffered sequential read would.
        "--threads", str(rg_threads),
        "-e", pattern,
    ]

    if needs_pcre2:
        cmd.extend(["--pcre2"])

    if use_glob:
        for g in _EXT_GLOBS:
            cmd.extend(["-g", g])
        # Also include files with double extensions like .txt.txt
        cmd.extend(["-g", "*.txt.*", "-g", "*.csv.*"])

    cmd.extend(targets)

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

        # Yielding one event per match means every single hit pays for a
        # full JSON encode + HTTP chunk round trip - with OR queries across
        # many terms on breach-sized data that can be tens of thousands of
        # matches, which turns into tens of thousands of tiny writes and
        # dominates wall-clock time far more than the actual disk I/O does.
        # Batch matches and flush on a small time/size window instead - UI
        # still feels live (updates several times a second) but at a
        # fraction of the overhead.
        FLUSH_EVERY_N = 50
        FLUSH_EVERY_SEC = 0.15
        last_flush = time.monotonic()

        def _progress_event():
            return {
                "type": "progress",
                "scanned_files": max(scanned_files_count, len(scanned_files_set)),
                "total_files": count_result["total_files"],
                "scanned_lines": max(estimated_lines, total_matches * 10),
                "total_matches": total_matches,
                "matches": batch
            }

        for line in proc.stdout:
            try:
                data = json.loads(line)
                msg_type = data.get("type")

                if msg_type == "end":
                    scanned_files_count += 1
                    estimated_lines += 250000
                    # File boundaries are much rarer than matches - always
                    # safe to flush here so the scanned-files counter keeps
                    # moving even during long gaps with no hits.
                    yield _progress_event()
                    batch = []
                    last_flush = time.monotonic()

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

                    now = time.monotonic()
                    if len(batch) >= FLUSH_EVERY_N or (now - last_flush) >= FLUSH_EVERY_SEC:
                        yield _progress_event()
                        batch = []
                        last_flush = now

                    if total_matches >= max_matches:
                        break

            except Exception:
                continue

        proc.terminate()

        yield {
            "type": "complete",
            "scanned_files": count_result["total_files"],
            "total_files": count_result["total_files"],
            "scanned_lines": max(estimated_lines, total_matches * 10),
            "total_matches": total_matches,
            "matches": batch
        }

    except Exception as e:
        if fallback is not None:
            yield from fallback()
        else:
            yield {"type": "error", "message": f"ripgrep failed: {e}"}


def _search_with_python(root_dir: str, query: str, mode: str, is_regex: bool, max_matches: int, count_result: Dict[str, Any]):
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
                                        "total_files": count_result["total_files"],
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
        "total_files": count_result["total_files"],
        "scanned_lines": scanned_lines,
        "total_matches": total_matches,
        "matches": batch
    }