"""
Persistent full-text index for Deadline, built on SQLite's FTS5 trigram
tokenizer.

Why this exists: fast_engine.py gets real search speed out of ripgrep, but
every single query still has to stream through the entire file tree on the
HDD from scratch - there is no way around the disk's raw throughput, no
matter how well ripgrep is tuned. For data that doesn't change every
search (which breach dumps / log archives generally don't), that's wasted
work: the same bytes get re-read on every query.

This module builds a one-time (or incremental) index that narrows a query
down to the small set of files that could possibly contain a match, using
an index lookup instead of a disk scan. ripgrep then runs its real,
line-accurate search only on that narrowed candidate list - so a query hits
a handful of files instead of the whole HDD.

Trigram tokenizer, not word tokenizer: the data here (emails, phone
numbers, SQL dumps, partial names) needs arbitrary substring matching, not
word-boundary matching. SQLite's FTS5 ships a `trigram` tokenizer
specifically for this - it indexes every overlapping 3-character sequence,
so a phrase query reconstructs into a substring match the same way `grep`
would, instead of silently requiring whole-word matches like a default
text-search analyzer would (that mismatch is the classic way a from-scratch
Elasticsearch setup goes wrong for this kind of data).
"""

import os
import re
import sqlite3
import time
import hashlib
from typing import Dict, Any, Generator, List, Optional

from fast_engine import VALID_EXTS, build_pattern, run_ripgrep_on_targets

INDEX_DIR_NAME = ".deadline_index"

# A trigram can't represent anything shorter than 3 literal characters, so
# a query term under this length can't be narrowed through the index at
# all - we fall back to a full scan for those rather than risk silently
# missing a match.
MIN_TRIGRAM_TERM_LEN = 3

# Content is indexed in line-aligned chunks rather than whole files, for two
# reasons: (1) some dumps contain single files many GB in size - reading one
# whole file into memory to index it would be a good way to run out of RAM,
# and (2) FTS5's trigram tokenizer generates one row entry per 3 bytes, so a
# single multi-GB row is also slow to build and to query. Splitting on line
# boundaries (never mid-line) avoids ever truncating a match across a chunk
# seam.
CHUNK_SIZE_BYTES = 8 * 1024 * 1024

# Trigram indexing a file costs roughly a few times its own size (every
# overlapping 3-byte window becomes an index entry) - cheap for the usual
# case of many small-to-medium files, but for a handful of multi-GB files it
# turns into a very slow build that bloats the index far past the point of
# being worth it. ripgrep alone can stream a single multi-GB file in
# seconds, so past this size there's nothing to gain from indexing content
# at all: the file is tracked as metadata only (instant - a stat() call, no
# read) and always included as a search target directly, no trigram lookup
# needed. The real win of the index is narrowing down which of the many
# SMALL files to check - that's unaffected by this cutoff.
MAX_INDEXED_CONTENT_BYTES = 64 * 1024 * 1024


def _index_db_path(root_dir: str) -> str:
    root_dir = os.path.abspath(root_dir)
    h = hashlib.sha1(root_dir.encode("utf-8")).hexdigest()[:16]
    # Deliberately NOT inside root_dir. When the indexed data lives on a
    # slow spinning HDD, writing the index database onto that SAME physical
    # disk means every index write forces the drive head off the sequential
    # read position of whatever source file is currently being read, and
    # back again - constant seeking instead of streaming, which is far
    # slower than either operation alone. Keeping the index on the system
    # drive (almost always an SSD) avoids that contention entirely, and as
    # a bonus doesn't eat into the capacity of a drive that's often full of
    # the actual dataset.
    base = os.path.join(os.path.expanduser("~"), INDEX_DIR_NAME)
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{h}.db")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def supports_trigram(conn: sqlite3.Connection) -> bool:
    """SQLite's trigram FTS5 tokenizer needs SQLite >= 3.34 (Dec 2020).
    Most modern Python builds bundle a new enough version, but this is
    cheap to verify rather than assume."""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _deadline_trigram_probe USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE _deadline_trigram_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS files_meta ("
        " path TEXT PRIMARY KEY,"
        " mtime REAL,"
        " size INTEGER"
        ")"
    )
    # Files over MAX_INDEXED_CONTENT_BYTES: metadata only, never
    # trigram-indexed. Always included as a direct ripgrep target at search
    # time instead.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS large_files ("
        " path TEXT PRIMARY KEY,"
        " mtime REAL,"
        " size INTEGER"
        ")"
    )
    # case_sensitive 0 mirrors the `-i` flag the ripgrep path already uses -
    # breach data is inconsistently cased and users expect case-insensitive
    # matching by default.
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5("
        " path UNINDEXED,"
        " chunk UNINDEXED,"
        " content,"
        " tokenize = 'trigram case_sensitive 0'"
        ")"
    )
    conn.commit()


def index_status(root_dir: str) -> Dict[str, Any]:
    db_path = _index_db_path(os.path.abspath(root_dir))
    if not os.path.exists(db_path):
        return {"exists": False, "indexed_files": 0}
    try:
        conn = _connect(db_path)
        n = conn.execute("SELECT COUNT(*) FROM files_meta").fetchone()[0]
        try:
            n_large = conn.execute("SELECT COUNT(*) FROM large_files").fetchone()[0]
        except sqlite3.OperationalError:
            n_large = 0
        conn.close()
        return {
            "exists": (n + n_large) > 0,
            "indexed_files": n,
            "large_files": n_large,
            "db_path": db_path
        }
    except Exception:
        return {"exists": False, "indexed_files": 0}


def _iter_valid_files(root_dir: str):
    for dirpath, dirnames, filenames in os.walk(root_dir):
        # Never index our own index database.
        dirnames[:] = [d for d in dirnames if d != INDEX_DIR_NAME]
        for f in filenames:
            _, ext = os.path.splitext(f)
            if ext.lower() in VALID_EXTS:
                yield os.path.join(dirpath, f)


def _index_one_file(conn: sqlite3.Connection, fp: str) -> None:
    conn.execute("DELETE FROM files_fts WHERE path = ?", (fp,))
    chunk_idx = 0
    buf_parts: List[bytes] = []
    buf_len = 0

    def flush():
        nonlocal chunk_idx, buf_parts, buf_len
        if not buf_parts:
            return
        text = b"".join(buf_parts).decode("utf-8", errors="replace")
        conn.execute(
            "INSERT INTO files_fts (path, chunk, content) VALUES (?, ?, ?)",
            (fp, chunk_idx, text)
        )
        chunk_idx += 1
        buf_parts = []
        buf_len = 0

    with open(fp, "rb") as fl:
        for raw_line in fl:
            buf_parts.append(raw_line)
            buf_len += len(raw_line)
            if buf_len >= CHUNK_SIZE_BYTES:
                flush()
        flush()


def build_index_generator(root_dir: str, force: bool = False) -> Generator[Dict[str, Any], None, None]:
    root_dir = os.path.abspath(root_dir)
    db_path = _index_db_path(root_dir)
    conn = _connect(db_path)

    if not supports_trigram(conn):
        conn.close()
        yield {
            "type": "error",
            "message": (
                "This Python's SQLite build doesn't support the FTS5 trigram "
                "tokenizer (needs SQLite 3.34+). Indexing isn't available - "
                "search will keep using the direct ripgrep scan."
            )
        }
        return

    _ensure_schema(conn)
    # This index is a disposable, rebuildable cache - trading durability for
    # bulk-insert speed is safe here.
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-262144")  # ~256MB page cache

    existing: Dict[str, Any] = {}
    existing_large: Dict[str, Any] = {}
    if not force:
        for path, mtime, size in conn.execute("SELECT path, mtime, size FROM files_meta"):
            existing[path] = (mtime, size)
        for path, mtime, size in conn.execute("SELECT path, mtime, size FROM large_files"):
            existing_large[path] = (mtime, size)

    # Walk and index in the same pass instead of collecting the full file
    # list first. On a multi-TB tree, materializing the whole list before
    # yielding anything meant the UI sat with zero feedback for however
    # long that walk took - which on a slow HDD with a huge tree can be
    # minutes, and looks exactly like the button "not responding". Now the
    # first progress event goes out almost immediately and total_files is
    # a running "files seen so far" count rather than a known-ahead total.
    yield {"type": "index_init", "total_files": 0}

    indexed = 0
    large = 0
    skipped = 0
    failed = 0
    seen = 0
    since_commit = 0
    since_checkpoint = 0
    COMMIT_EVERY = 200
    # Checkpointing folds the WAL back into the main .db file and resets its
    # size. Without this, synchronous=OFF + a long build means the WAL just
    # grows without bound for the whole run - on one real dataset this hit
    # multiple GB of WAL for a single large file before anything was ever
    # folded back in, making the build look stuck with no visible progress.
    CHECKPOINT_EVERY = 2000
    last_emit = time.monotonic()

    for fp in _iter_valid_files(root_dir):
        seen += 1
        try:
            st = os.stat(fp)
            if st.st_size > MAX_INDEXED_CONTENT_BYTES:
                prev = existing_large.get(fp)
                if prev is not None and prev[0] == st.st_mtime and prev[1] == st.st_size:
                    skipped += 1
                else:
                    conn.execute(
                        "INSERT INTO large_files (path, mtime, size) VALUES (?, ?, ?) "
                        "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size",
                        (fp, st.st_mtime, st.st_size)
                    )
                    # A file that shrank below the threshold since a
                    # previous run may still have stale trigram content -
                    # safe to always clear it when (re)classifying as large.
                    conn.execute("DELETE FROM files_fts WHERE path = ?", (fp,))
                    conn.execute("DELETE FROM files_meta WHERE path = ?", (fp,))
                    large += 1
                    since_commit += 1
            else:
                prev = existing.get(fp)
                if prev is not None and prev[0] == st.st_mtime and prev[1] == st.st_size:
                    skipped += 1
                else:
                    _index_one_file(conn, fp)
                    conn.execute(
                        "INSERT INTO files_meta (path, mtime, size) VALUES (?, ?, ?) "
                        "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size",
                        (fp, st.st_mtime, st.st_size)
                    )
                    # A file that grew past the threshold since a previous
                    # run may still be registered as "large" - clear that.
                    conn.execute("DELETE FROM large_files WHERE path = ?", (fp,))
                    indexed += 1
                    since_commit += 1
        except Exception:
            failed += 1

        if since_commit >= COMMIT_EVERY:
            conn.commit()
            since_commit = 0
            since_checkpoint += COMMIT_EVERY
            if since_checkpoint >= CHECKPOINT_EVERY:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                since_checkpoint = 0

        now = time.monotonic()
        if now - last_emit >= 0.2:
            yield {
                "type": "index_progress",
                "total_files": seen,
                "indexed_files": indexed,
                "large_files": large,
                "skipped_files": skipped,
                "failed_files": failed,
                "done_files": indexed + large + skipped + failed
            }
            last_emit = now

    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.commit()
    conn.close()

    yield {
        "type": "index_complete",
        "total_files": seen,
        "indexed_files": indexed,
        "large_files": large,
        "skipped_files": skipped,
        "failed_files": failed
    }


def search_index_generator(root_dir: str, query: str, mode: str = "OR", is_regex: bool = False,
                            max_matches: int = 100000) -> Generator[Dict[str, Any], None, None]:
    root_dir = os.path.abspath(root_dir)
    db_path = _index_db_path(root_dir)

    # Regex can't generally be narrowed through a trigram index (it would
    # need literal substrings extracted from the pattern first) - the
    # direct ripgrep scan is the correct, always-right behavior here.
    # Same when no index exists yet for this path.
    if is_regex or not os.path.exists(db_path):
        from fast_engine import search_files_generator
        yield from search_files_generator(root_dir, query, mode, is_regex, max_matches)
        return

    terms = [p.strip() for p in re.split(r'[|,،\n]+', query) if p.strip()]
    if not terms or any(len(t) < MIN_TRIGRAM_TERM_LEN for t in terms):
        from fast_engine import search_files_generator
        yield from search_files_generator(root_dir, query, mode, is_regex, max_matches)
        return

    try:
        conn = _connect(db_path)
    except Exception:
        from fast_engine import search_files_generator
        yield from search_files_generator(root_dir, query, mode, is_regex, max_matches)
        return

    joiner = " AND " if mode == "AND" else " OR "
    fts_query = joiner.join('"' + t.replace('"', '""') + '"' for t in terms)

    t0 = time.monotonic()
    try:
        rows = conn.execute(
            "SELECT DISTINCT path FROM files_fts WHERE files_fts MATCH ?", (fts_query,)
        ).fetchall()
        # Large files were never content-indexed (see
        # MAX_INDEXED_CONTENT_BYTES) - there's no way to know from the index
        # alone whether they'd match, so they're always included as direct
        # ripgrep targets. This is cheap: by construction there are few of
        # them, and ripgrep streams even a multi-GB file in seconds.
        try:
            large_rows = conn.execute("SELECT path FROM large_files").fetchall()
        except sqlite3.OperationalError:
            large_rows = []
    except sqlite3.OperationalError:
        # Index exists but the query didn't parse against it for some
        # reason - fail open to the always-correct full scan rather than
        # erroring out.
        conn.close()
        from fast_engine import search_files_generator
        yield from search_files_generator(root_dir, query, mode, is_regex, max_matches)
        return
    finally:
        conn.close()

    candidate_set = {r[0] for r in rows} | {r[0] for r in large_rows}
    candidates = [p for p in candidate_set if os.path.exists(p)]
    lookup_seconds = round(time.monotonic() - t0, 3)

    pattern, needs_pcre2_or_err = build_pattern(query, mode, is_regex)
    if pattern is None:
        yield {"type": "error", "message": needs_pcre2_or_err}
        return
    needs_pcre2 = bool(needs_pcre2_or_err) and mode == "AND" and not is_regex

    count_result: Dict[str, Any] = {"total_files": len(candidates), "done": True}
    yield {
        "type": "init",
        "total_files": len(candidates),
        "via_index": True,
        "index_lookup_seconds": lookup_seconds
    }

    yield from run_ripgrep_on_targets(
        targets=candidates, pattern=pattern, needs_pcre2=needs_pcre2,
        max_matches=max_matches, count_result=count_result, root_dir=root_dir,
        use_glob=False
    )
