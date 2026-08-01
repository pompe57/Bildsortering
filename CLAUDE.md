# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Setup (venv already exists at `.venv/`, packages: Pillow, ExifRead):
```
.venv/Scripts/python.exe -m pip install Pillow exifread
```

Run the scanner (scans one or two photo source folders into a SQLite DB):
```
.venv/Scripts/python.exe scan_bilder_v2.py --källa1 "C:\path\to\source1" --namn1 "OneDrive" --db "onedrive.db"

.venv/Scripts/python.exe scan_bilder_v2.py --källa1 "C:\path\to\source1" --namn1 "OneDrive" ^
                                            --källa2 "Z:\path\to\source2" --namn2 "NAS" ^
                                            --db "bilder.db"
```

There is no test suite, linter, or build step in this repo yet — it's a single standalone script.

## Architecture

This is a single-script tool (`scan_bilder_v2.py`) that indexes photo archives into a SQLite database ahead of a manual dedup/cleanup pass. It does not modify or move any photo files — it only reads and records metadata.

**Pairing logic**: files are grouped by `(relative folder, filename stem)` so a JPEG and its RAW sibling (`.cr2`/`.arw`) sharing the same base name land in one `bilder` row (`har_jpeg`/`har_raw` flags, separate `sökväg_jpeg`/`sökväg_raw` columns). A photo can exist as JPEG-only, RAW-only, or both.

**Per-image processing** (`lägg_in_bilder`): for each JPEG, EXIF is read via Pillow (`läs_exif`) — camera make/model, dimensions, ISO/shutter/aperture, GPS (converted from DMS to decimal degrees), and capture date (parsed into separate `år`/`månad` columns for filtering). An MD5 of the JPEG bytes is computed (`md5_hash`) for exact-duplicate detection; **on any read error this silently returns `""`** rather than `None` or raising — worth checking when relying on `hash_jpeg` for completeness, since a file with a failed hash is indistinguishable from one that was never hashed unless you check for `''` specifically.

**Cross-source analysis** (`korsanalys`): only runs when two sources are scanned in the same invocation. It self-joins `bilder` across the two `källa_id`s to populate the `kopior` table with two match types: `exakt` (identical MD5) and `samma_namn` (same base filename, different hash — likely an edited/re-exported version worth a manual look).

**Database schema** (created fresh via `SCHEMA` in the script, `CREATE TABLE IF NOT EXISTS`, so re-running against an existing DB file adds to it rather than resetting it):
- `källor` — one row per scanned source folder (name, path, scan timestamp)
- `bilder` — one row per JPEG/RAW pair or singleton, FK to `källor`
- `kopior` — pairwise duplicate/near-duplicate links between `bilder` rows, FK pair + `match_typ`

Re-running the script against the same `--db` file accumulates new `källor`/`bilder` rows rather than replacing existing data — there's no dedup-on-rescan of a previously scanned source.

`skriv_sammanfattning` prints a human-readable report at the end of every run (counts per source, cross-source overlap, per-camera and per-year breakdowns) and, when exactly two sources are loaded, some ready-to-paste SQL queries for manual review in a SQLite browser.

## Project context

Long-term goal: end up with a cleaned, tagged photo archive — captions explaining what's in each kept photo, and bad shots/duplicates/near-duplicate burst shots sent to "the big bitbucket in the sky."

Phase plan:
- **Fas 1 — Inventering ✅ Klar**: index all photos from all sources into SQLite (this script). EXIF, camera info, date, GPS.
- **Fas 2 — Rensning (next)**: MD5 exact duplicates removed; perceptual hashing to identify burst sequences and near-duplicates; manual review of candidates before sending to the bitbucket.
- **Fas 3 — Taggning**: automatic tagging via an AI vision API (send an image, get back a description like "Two people at a dinner table, indoors, summer, likely 2010s"); `imagehash` for visual grouping; manual completion/correction of tags in the database.
- **Fas 4 — Master-bibliotek**: consolidate onto one source (likely a LaCie drive) as the master copy — cleaned, tagged, searchable. Possibly a small custom search UI on top of the SQLite db.

Planned module layout (only `scan_bilder_v2.py` exists so far; the rest are Fas 2/3/4 work, not yet written):
```
Bildsortering/
├── scan_bilder_v2.py      # ✅ Klar — Fas 1, inventory scan
├── phash_index.py         # Fas 2 — compute perceptual hash
├── duplicates.py          # Fas 2 — find and report duplicates
├── ai_tagger.py           # Fas 3 — AI-based image tagging
├── cleanup.py             # Fas 2/4 — move/delete approved candidates
├── requirements.txt
├── CLAUDE.md
└── onedrive.db            # the database (gitignored, machine-local)
```
