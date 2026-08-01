"""
scan_bilder_v2.py
=================
Skannar två bildkällor (t.ex. OneDrive + NAS) och bygger en SQLite-databas
med JPEG+RAW-par (CR2, ARW), EXIF-data, dubblettdetektering och korsanalys.

Beroenden:
    pip install Pillow exifread

Körning — skanna båda källorna:
    python scan_bilder_v2.py --källa1 "C:\\Users\\Per\\OneDrive\\Bilder" --namn1 "OneDrive" ^
                              --källa2 "Z:\\Bilder" --namn2 "NAS" ^
                              --db "bilder.db"

Körning — skanna bara en källa (kan komplettera senare):
    python scan_bilder_v2.py --källa1 "C:\\Users\\Per\\OneDrive\\Bilder" --namn1 "OneDrive" ^
                              --db "bilder.db"
"""

import os
import sys
import hashlib
import sqlite3
import argparse
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image
    from PIL.ExifTags import TAGS, GPSTAGS
except ImportError:
    print("Saknade bibliotek. Kör: pip install Pillow exifread")
    sys.exit(1)


# ── Filtyper ──────────────────────────────────────────────────────────────────
JPEG_EXT = {".jpg", ".jpeg"}
RAW_EXT  = {".cr2", ".arw"}
ALLA_EXT  = JPEG_EXT | RAW_EXT


# ── Databas-schema ────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS källor (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    namn     TEXT NOT NULL,
    sökväg   TEXT NOT NULL,
    skannad  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bilder (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    källa_id         INTEGER NOT NULL REFERENCES källor(id),
    bas_namn         TEXT NOT NULL,
    mapp             TEXT NOT NULL,          -- relativ mappsökväg inom källan
    sökväg_jpeg      TEXT,
    sökväg_raw       TEXT,
    raw_format       TEXT,                   -- CR2 eller ARW
    har_jpeg         INTEGER DEFAULT 0,
    har_raw          INTEGER DEFAULT 0,
    filstorlek_jpeg  INTEGER,
    filstorlek_raw   INTEGER,
    datum_tagen      TEXT,
    år               INTEGER,
    månad            INTEGER,
    kamera_märke     TEXT,
    kamera_modell    TEXT,
    bredd            INTEGER,
    höjd             INTEGER,
    iso              INTEGER,
    slutartid        TEXT,
    bländare         TEXT,
    gps_lat          REAL,
    gps_lon          REAL,
    hash_jpeg        TEXT,
    importerad       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_källa   ON bilder(källa_id);
CREATE INDEX IF NOT EXISTS idx_datum   ON bilder(datum_tagen);
CREATE INDEX IF NOT EXISTS idx_kamera  ON bilder(kamera_modell);
CREATE INDEX IF NOT EXISTS idx_hash    ON bilder(hash_jpeg);
CREATE INDEX IF NOT EXISTS idx_år      ON bilder(år, månad);
CREATE INDEX IF NOT EXISTS idx_bas     ON bilder(bas_namn);

CREATE TABLE IF NOT EXISTS kopior (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bild_id_1   INTEGER NOT NULL REFERENCES bilder(id),
    bild_id_2   INTEGER NOT NULL REFERENCES bilder(id),
    match_typ   TEXT NOT NULL
    -- 'exakt'         = identisk hash, äkta kopia
    -- 'samma_namn'    = samma basnamn, olika hash (olika version/redigerad)
);
"""


# ── EXIF-hjälpare ─────────────────────────────────────────────────────────────
def _gps_grader(värde):
    try:
        d = float(värde[0])
        m = float(värde[1])
        s = float(värde[2])
        return d + m / 60 + s / 3600
    except Exception:
        return None


def läs_exif(sökväg: Path) -> dict:
    info = {}
    try:
        with Image.open(sökväg) as img:
            info["bredd"], info["höjd"] = img.size
            raw_exif = img._getexif()
            if not raw_exif:
                return info
            exif = {TAGS.get(k, k): v for k, v in raw_exif.items()}

            for fält in ("DateTimeOriginal", "DateTime", "DateTimeDigitized"):
                if fält in exif:
                    info["datum_tagen"] = exif[fält]
                    break

            info["kamera_märke"]  = exif.get("Make", "").strip()
            info["kamera_modell"] = exif.get("Model", "").strip()
            info["iso"]           = exif.get("ISOSpeedRatings")
            ev = exif.get("ExposureTime")
            info["slutartid"]     = str(ev) if ev else None
            ap = exif.get("FNumber")
            info["bländare"]      = f"f/{float(ap):.1f}" if ap else None

            gps_raw = exif.get("GPSInfo")
            if gps_raw:
                gps = {GPSTAGS.get(k, k): v for k, v in gps_raw.items()}
                lat = _gps_grader(gps.get("GPSLatitude", []))
                lon = _gps_grader(gps.get("GPSLongitude", []))
                if lat and lon:
                    if gps.get("GPSLatitudeRef") == "S":
                        lat = -lat
                    if gps.get("GPSLongitudeRef") == "W":
                        lon = -lon
                    info["gps_lat"] = lat
                    info["gps_lon"] = lon
    except Exception as e:
        print(f"  [EXIF-fel] {sökväg.name}: {e}")
    return info


def md5_hash(sökväg: Path, chunk=65536) -> str:
    h = hashlib.md5()
    try:
        with open(sökväg, "rb") as f:
            while True:
                block = f.read(chunk)
                if not block:
                    break
                h.update(block)
        return h.hexdigest()
    except Exception:
        return ""


# ── Skanning ──────────────────────────────────────────────────────────────────
def skanna_mapp(rotmapp: Path) -> dict:
    """Returnerar dict: (mapp_rel, basnamn) → {jpeg, raw, raw_format}"""
    grupper = {}
    totalt  = 0

    for fil in rotmapp.rglob("*"):
        if not fil.is_file():
            continue
        ext = fil.suffix.lower()
        if ext not in ALLA_EXT:
            continue
        totalt += 1
        mapp_rel = fil.parent.relative_to(rotmapp)
        nyckel   = (str(mapp_rel), fil.stem)

        if nyckel not in grupper:
            grupper[nyckel] = {"jpeg": None, "raw": None, "raw_format": None}

        if ext in JPEG_EXT:
            grupper[nyckel]["jpeg"] = fil
        elif ext in RAW_EXT:
            grupper[nyckel]["raw"]        = fil
            grupper[nyckel]["raw_format"] = ext.lstrip(".").upper()

    print(f"  Hittade {totalt} filer → {len(grupper)} bildpar/bilder")
    return grupper


def lägg_in_bilder(con: sqlite3.Connection, grupper: dict, källa_id: int) -> dict:
    """
    Infogar bilder för en källa.
    Returnerar hash_index: {hash_jpeg: bild_id} för korsanalys.
    """
    cur       = con.cursor()
    nu        = datetime.now().isoformat()
    hash_index = {}
    antal     = 0

    for (mapp_rel, bas_namn), filer in grupper.items():
        jpeg_path = filer["jpeg"]
        raw_path  = filer["raw"]

        exif            = {}
        hash_jpeg       = None
        filstorlek_jpeg = None
        filstorlek_raw  = None

        if jpeg_path:
            exif            = läs_exif(jpeg_path)
            hash_jpeg       = md5_hash(jpeg_path)
            filstorlek_jpeg = jpeg_path.stat().st_size
        if raw_path:
            filstorlek_raw = raw_path.stat().st_size

        år = månad = None
        datum = exif.get("datum_tagen")
        if datum:
            try:
                dt    = datetime.strptime(datum, "%Y:%m:%d %H:%M:%S")
                år    = dt.year
                månad = dt.month
            except Exception:
                pass

        cur.execute("""
            INSERT INTO bilder (
                källa_id, bas_namn, mapp,
                sökväg_jpeg, sökväg_raw, raw_format,
                har_jpeg, har_raw,
                filstorlek_jpeg, filstorlek_raw,
                datum_tagen, år, månad,
                kamera_märke, kamera_modell,
                bredd, höjd, iso, slutartid, bländare,
                gps_lat, gps_lon, hash_jpeg, importerad
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            källa_id, bas_namn, str(mapp_rel),
            str(jpeg_path) if jpeg_path else None,
            str(raw_path)  if raw_path  else None,
            filer["raw_format"],
            1 if jpeg_path else 0,
            1 if raw_path  else 0,
            filstorlek_jpeg, filstorlek_raw,
            datum, år, månad,
            exif.get("kamera_märke"), exif.get("kamera_modell"),
            exif.get("bredd"), exif.get("höjd"),
            exif.get("iso"), exif.get("slutartid"), exif.get("bländare"),
            exif.get("gps_lat"), exif.get("gps_lon"),
            hash_jpeg, nu,
        ))

        bild_id = cur.lastrowid
        antal  += 1

        if hash_jpeg:
            hash_index[hash_jpeg] = bild_id

        if antal % 100 == 0:
            print(f"  ... {antal} bilder inlagda")
            con.commit()

    con.commit()
    print(f"  ✓ {antal} bilder inlagda för denna källa")
    return hash_index


# ── Korsanalys ────────────────────────────────────────────────────────────────
def korsanalys(con: sqlite3.Connection, källa_id_1: int, källa_id_2: int):
    """Jämför de två källorna och fyller kopior-tabellen."""
    cur = con.cursor()

    # 1) Exakta kopior — samma hash
    cur.execute("""
        INSERT INTO kopior (bild_id_1, bild_id_2, match_typ)
        SELECT a.id, b.id, 'exakt'
        FROM bilder a
        JOIN bilder b ON a.hash_jpeg = b.hash_jpeg
        WHERE a.källa_id = ? AND b.källa_id = ?
          AND a.hash_jpeg IS NOT NULL
    """, (källa_id_1, källa_id_2))

    exakta = cur.rowcount

    # 2) Samma basnamn men olika hash — möjliga versioner
    cur.execute("""
        INSERT INTO kopior (bild_id_1, bild_id_2, match_typ)
        SELECT a.id, b.id, 'samma_namn'
        FROM bilder a
        JOIN bilder b ON a.bas_namn = b.bas_namn
        WHERE a.källa_id = ? AND b.källa_id = ?
          AND (a.hash_jpeg != b.hash_jpeg
               OR a.hash_jpeg IS NULL
               OR b.hash_jpeg IS NULL)
    """, (källa_id_1, källa_id_2))

    samma_namn = cur.rowcount
    con.commit()

    print(f"\n  Korsanalys klar:")
    print(f"    Exakta kopior          : {exakta}")
    print(f"    Samma namn, olika hash : {samma_namn}")


# ── Sammanfattning ─────────────────────────────────────────────────────────────
def skriv_sammanfattning(con: sqlite3.Connection):
    cur = con.cursor()
    print("\n── Sammanfattning ──────────────────────────────────────────────")

    cur.execute("SELECT id, namn, sökväg FROM källor")
    källor = cur.fetchall()

    for källa_id, namn, sökväg in källor:
        print(f"\n  [{namn}]  {sökväg}")
        cur.execute("SELECT COUNT(*) FROM bilder WHERE källa_id=?", (källa_id,))
        print(f"    Totalt bildpar/bilder  : {cur.fetchone()[0]}")
        cur.execute("SELECT COUNT(*) FROM bilder WHERE källa_id=? AND har_jpeg=1 AND har_raw=1", (källa_id,))
        print(f"    Kompletta par (J+RAW)  : {cur.fetchone()[0]}")
        cur.execute("SELECT COUNT(*) FROM bilder WHERE källa_id=? AND har_jpeg=1 AND har_raw=0", (källa_id,))
        print(f"    Bara JPEG              : {cur.fetchone()[0]}")
        cur.execute("SELECT COUNT(*) FROM bilder WHERE källa_id=? AND har_raw=1 AND har_jpeg=0", (källa_id,))
        print(f"    Bara RAW               : {cur.fetchone()[0]}")

    if len(källor) == 2:
        id1, namn1, _ = källor[0]
        id2, namn2, _ = källor[1]
        print(f"\n  Korsanalys {namn1} ↔ {namn2}:")
        cur.execute("SELECT COUNT(*) FROM kopior WHERE match_typ='exakt'")
        print(f"    Exakta kopior          : {cur.fetchone()[0]}")
        cur.execute("SELECT COUNT(*) FROM kopior WHERE match_typ='samma_namn'")
        print(f"    Samma namn, diff hash  : {cur.fetchone()[0]}")

        # Unika per källa
        cur.execute("""
            SELECT COUNT(*) FROM bilder
            WHERE källa_id=? AND (hash_jpeg IS NULL OR hash_jpeg NOT IN
                (SELECT hash_jpeg FROM bilder WHERE källa_id=? AND hash_jpeg IS NOT NULL))
        """, (id1, id2))
        print(f"    Bara på {namn1:<12}  : {cur.fetchone()[0]}")

        cur.execute("""
            SELECT COUNT(*) FROM bilder
            WHERE källa_id=? AND (hash_jpeg IS NULL OR hash_jpeg NOT IN
                (SELECT hash_jpeg FROM bilder WHERE källa_id=? AND hash_jpeg IS NOT NULL))
        """, (id2, id1))
        print(f"    Bara på {namn2:<12}  : {cur.fetchone()[0]}")

    print("\n  Bilder per kamera (totalt):")
    cur.execute("""
        SELECT kamera_modell, COUNT(*) n FROM bilder
        WHERE kamera_modell IS NOT NULL AND kamera_modell != ''
        GROUP BY kamera_modell ORDER BY n DESC
    """)
    for rad in cur.fetchall():
        print(f"    {rad[0]:<35} {rad[1]}")

    print("\n  Bilder per år (totalt):")
    cur.execute("""
        SELECT år, COUNT(*) n FROM bilder
        WHERE år IS NOT NULL GROUP BY år ORDER BY år
    """)
    for rad in cur.fetchall():
        print(f"    {rad[0]}  →  {rad[1]}")

    print("────────────────────────────────────────────────────────────────")
    print("\n  Användbara queries i DB Browser:")
    print("""
  -- Vad finns BARA på OneDrive?
  SELECT bas_namn, sökväg_jpeg FROM bilder
  WHERE källa_id = 1
  AND (hash_jpeg NOT IN (SELECT hash_jpeg FROM bilder WHERE källa_id = 2)
       OR hash_jpeg IS NULL);

  -- Vad finns BARA på NAS?
  SELECT bas_namn, sökväg_jpeg FROM bilder
  WHERE källa_id = 2
  AND (hash_jpeg NOT IN (SELECT hash_jpeg FROM bilder WHERE källa_id = 1)
       OR hash_jpeg IS NULL);

  -- Exakta kopior med sökvägar
  SELECT b1.sökväg_jpeg, b2.sökväg_jpeg
  FROM kopior k
  JOIN bilder b1 ON k.bild_id_1 = b1.id
  JOIN bilder b2 ON k.bild_id_2 = b2.id
  WHERE k.match_typ = 'exakt';

  -- Samma basnamn men olika filer (kontrollera manuellt)
  SELECT b1.sökväg_jpeg, b2.sökväg_jpeg,
         b1.filstorlek_jpeg, b2.filstorlek_jpeg
  FROM kopior k
  JOIN bilder b1 ON k.bild_id_1 = b1.id
  JOIN bilder b2 ON k.bild_id_2 = b2.id
  WHERE k.match_typ = 'samma_namn';
    """)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Skannar två bildkällor till SQLite")
    parser.add_argument("--källa1", required=True, help="Sökväg till källa 1 (t.ex. OneDrive)")
    parser.add_argument("--namn1",  default="Källa1", help="Namn på källa 1")
    parser.add_argument("--källa2", help="Sökväg till källa 2 (t.ex. NAS) — valfri")
    parser.add_argument("--namn2",  default="Källa2", help="Namn på källa 2")
    parser.add_argument("--db",     default="bilder.db", help="SQLite-fil (default: bilder.db)")
    args = parser.parse_args()

    db_sökväg = Path(args.db)
    con = sqlite3.connect(db_sökväg)
    con.executescript(SCHEMA)

    nu = datetime.now().isoformat()

    # Källa 1
    rot1 = Path(args.källa1)
    if not rot1.exists():
        print(f"Fel: '{rot1}' hittades inte.")
        sys.exit(1)

    print(f"\n[1/2] Skannar {args.namn1}: {rot1}")
    con.execute("INSERT INTO källor (namn, sökväg, skannad) VALUES (?,?,?)",
                (args.namn1, str(rot1), nu))
    källa_id_1 = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.commit()

    grupper1    = skanna_mapp(rot1)
    hash_idx_1  = lägg_in_bilder(con, grupper1, källa_id_1)

    källa_id_2 = None
    if args.källa2:
        rot2 = Path(args.källa2)
        if not rot2.exists():
            print(f"Fel: '{rot2}' hittades inte.")
            sys.exit(1)

        print(f"\n[2/2] Skannar {args.namn2}: {rot2}")
        con.execute("INSERT INTO källor (namn, sökväg, skannad) VALUES (?,?,?)",
                    (args.namn2, str(rot2), nu))
        källa_id_2 = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.commit()

        grupper2 = skanna_mapp(rot2)
        lägg_in_bilder(con, grupper2, källa_id_2)

        print("\nKör korsanalys ...")
        korsanalys(con, källa_id_1, källa_id_2)
    else:
        print("\n(Ingen källa 2 angiven — kör igen med --källa2 för korsanalys)")

    skriv_sammanfattning(con)
    con.close()
    print(f"\nDatabas sparad: {db_sökväg}\n")


if __name__ == "__main__":
    main()
