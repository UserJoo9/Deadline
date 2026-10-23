<div align="center">

# ⚡ DEADLINE

**High-performance local full-text search over terabyte-scale file sets**

<img src="https://img.shields.io/badge/Status-Active-00ff66?style=for-the-badge" alt="Status">
<img src="https://img.shields.io/badge/Python-3.10+-00f0ff?style=for-the-badge&logo=python" alt="Python">
<img src="https://img.shields.io/badge/Engine-Ripgrep%20%2B%20SQLite%20FTS5-ff0055?style=for-the-badge" alt="Engine">
<img src="https://img.shields.io/badge/Platform-Windows-0078D6?style=for-the-badge&logo=windows" alt="Platform">
<img src="https://img.shields.io/badge/UI-Cyberpunk%20Terminal-00ff66?style=for-the-badge" alt="Theme">

**[🇪🇬 اقرأ بالعربي](README.ar.md)**

</div>

---

A local, offline-first search engine for scanning huge mixed-format file sets (`.txt`, `.csv`, `.sql`, `.json`, `.log`, and more) stored on slow HDD storage — fast.

## Features

- **Ripgrep core** — multi-threaded, Rust-powered line search.
- **Persistent SQLite FTS5 (trigram) index** — build it once, and every search after that hits a small candidate list instead of re-scanning the whole drive. Trigram tokenization means real **substring matching** (partial emails, phone numbers, fragments) — not just whole-word matches. Files over 64MB are tracked as metadata only and always scanned directly (indexing their content isn't worth it — ripgrep streams a multi-GB file in seconds on its own).
- **Automatic safe fallback** — no index yet, a search term under 3 characters, or Regex mode all transparently fall back to a direct full scan. Results are never silently incomplete.
- **Live streaming results (SSE-style)** — matches appear as they're found, no waiting for the scan to finish.
- **OR / AND / Regex search modes**, bilingual UI (AR/EN), JSON export, copy-to-clipboard.

## Requirements

1. Python 3.10+
2. [Ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) on your PATH — `scoop install ripgrep` or `choco install ripgrep`
3. `pip install fastapi uvicorn`

## Running it

```bash
python server.py
```
or just double-click `start_deadline.bat`. Opens automatically at `http://127.0.0.1:8000`.

## Usage tips

- Type multiple terms separated by `|` or a comma: `01012345678 | admin@target.com | John Doe`
- Set the **Path** field to any folder, local or external drive.
- Click **Build Index** once per dataset for dramatically faster repeat searches (first build reads everything once, like a scan; it's worth it for data you search more than once). It updates incrementally afterward — only changed files get re-indexed.
- The index lives under your home directory, not inside the scanned folder, so it doesn't compete with the scan for disk I/O and doesn't eat into that drive's space.
- Needs ≥3-character terms to use the index; shorter terms and Regex queries always do a full scan.

## Disclaimer

Built for authorized security research, penetration testing, and OSINT data analysis. Use is entirely the user's responsibility.
