"""Xeno-canto fetcher: --probe (counts only) and tier downloads."""
import argparse
import csv
import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
API = "https://xeno-canto.org/api/3/recordings"
USER_AGENT = "animal-noise-catalogue/0.1 (personal sample-library research; contact: wonderburg7@gmail.com)"
DELAY_S = 1.0
MAX_PAGES = 3  # pages of results ranked per query
RAW_DIR = ROOT / "data" / "raw" / "xc"
STATE = ROOT / "data" / "state" / "xc.json"
RAW_MANIFEST = ROOT / "data" / "raw_manifest.csv"
MANIFEST_COLS = ["source", "source_id", "tier", "query_id", "role_hint", "ultrasonic",
                 "recordist", "licence", "licence_url", "source_url", "species_sci",
                 "species_en", "grp", "call_type", "quality", "smp", "length_s", "also", "file"]

log = logging.getLogger("fetch_xc")


def build_query(cfg, tier_filter, q):
    return " ".join([cfg["base_query"], tier_filter, q])


def probe(cfg, tiers, limit, dry_run, key):
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    queries = cfg["queries"][:limit] if limit else cfg["queries"]
    rows, failed, first = [], 0, True
    for qd in queries:
        for tier in tiers:
            query = build_query(cfg, cfg["tiers"][tier], qd["q"])
            row = {"id": qd["id"], "tier": tier, "query": query,
                   "num_recordings": "", "num_species": "", "note": ""}
            if dry_run:
                row["note"] = "dry-run"
                rows.append(row)
                continue
            if not first:
                time.sleep(DELAY_S)
            first = False
            try:
                r = session.get(API, params={"query": query, "key": key}, timeout=30)
                log.info("%s %s -> HTTP %s", qd["id"], tier, r.status_code)
                if r.status_code in (401, 403, 429):
                    row["note"] = f"HTTP {r.status_code}, aborting"
                    rows.append(row)
                    failed += 1
                    return rows, failed
                r.raise_for_status()
                data = r.json()
                row["num_recordings"] = int(data["numRecordings"])
                row["num_species"] = int(data["numSpecies"])
                if row["num_recordings"] == 0:
                    row["note"] = "ZERO"
            except Exception as e:  # report, do not skip silently
                failed += 1
                row["note"] = f"error: {type(e).__name__}"
                log.exception("%s %s failed", qd["id"], tier)
            rows.append(row)
    return rows, failed


def licence_of(url):
    """'https://creativecommons.org/licenses/by-nc-nd/4.0/' -> 'by-nc-nd'."""
    if url.startswith("//"):
        url = "https:" + url
    parts = [x for x in urlparse(url).path.split("/") if x]
    return parts[1].lower() if len(parts) >= 2 and parts[0] == "licenses" else ""


def length_s(text):
    try:
        secs = 0
        for p in str(text).split(":"):
            secs = secs * 60 + int(p)
        return secs
    except ValueError:
        return None


def sniff_ext(head):
    if head[:4] == b"RIFF":
        return "wav"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3"
    return None


def fetch_pool(session, cfg, tier, qd, key):
    """Return (ranked candidates, n_licence_skipped) for one query."""
    query = build_query(cfg, cfg["tiers"][tier], qd["q"])
    recs, page, pages = [], 1, 1
    while page <= min(pages, MAX_PAGES):
        time.sleep(DELAY_S)
        r = session.get(API, params={"query": query, "key": key, "page": page}, timeout=30)
        log.info("%s %s page %s -> HTTP %s", qd["id"], tier, page, r.status_code)
        r.raise_for_status()
        data = r.json()
        pages = int(data.get("numPages", 1))
        recs += data.get("recordings", [])
        page += 1
    out, lic_skipped = [], 0
    for rec in recs:
        lic = licence_of(rec.get("lic", ""))
        if not lic or "nc" in lic.split("-") or "nd" in lic.split("-"):
            lic_skipped += 1
            continue
        length = length_s(rec.get("length"))
        try:
            smp = int(rec.get("smp") or 0)
        except ValueError:
            smp = 0
        if rec.get("q") in ("D", "E") or smp < 22050 or length is None or length > cfg["max_len_s"]:
            continue
        rec["_lic"], rec["_len"], rec["_smp"] = lic, length, smp
        out.append(rec)
    # quality (A best) outranks everything; then clean recordings (no `also`), then sample rate
    out.sort(key=lambda x: (x.get("q") or "Z",
                            bool(x.get("also")) if cfg.get("prefer_no_also") else 0,
                            -x["_smp"]))
    return out, lic_skipped


def select(cfg, tier, key, session, limit, bad):
    """Round-robin over queries so a small --limit still spans roles."""
    d = cfg.get("defaults", {})
    pools, lic_skipped = [], 0
    for qd in cfg["queries"]:
        cands, sk = fetch_pool(session, cfg, tier, qd, key)
        lic_skipped += sk
        pools.append({"qd": qd, "cands": cands, "n": 0, "sp": {},
                      "max": qd.get("max_recordings", d.get("max_recordings", 40)),
                      "per_sp": qd.get("max_per_species", d.get("max_per_species", 2))})
    chosen, seen = [], set()
    progress = True
    while progress and (not limit or len(chosen) < limit):
        progress = False
        for p in pools:
            if limit and len(chosen) >= limit:
                break
            while p["cands"] and p["n"] < p["max"]:
                rec = p["cands"].pop(0)
                sp = f"{rec.get('gen')} {rec.get('sp')}"
                if rec["id"] in seen or str(rec["id"]) in bad or p["sp"].get(sp, 0) >= p["per_sp"]:
                    continue
                seen.add(rec["id"])
                p["sp"][sp] = p["sp"].get(sp, 0) + 1
                p["n"] += 1
                chosen.append((p["qd"], rec))
                progress = True
                break
    return chosen, lic_skipped


def download(session, rec):
    """Fetch the original and return its local Path."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RAW_DIR / f"XC{rec['id']}.part"
    url = rec.get("file") or f"https://xeno-canto.org/{rec['id']}/download"
    with session.get(url, stream=True, timeout=120, allow_redirects=True) as r:
        r.raise_for_status()
        it = r.iter_content(65536)
        first = next(it, b"")
        ext = sniff_ext(first)
        if ext is None:
            raise ValueError("unrecognised audio content")
        with tmp.open("wb") as f:
            f.write(first)
            for chunk in it:
                f.write(chunk)
    dest = RAW_DIR / f"XC{rec['id']}.{ext}"
    tmp.replace(dest)
    return dest


def run_fetch(cfg, tier, limit, dry_run, key):
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    bad = {k for k, v in state.items() if v.get("failed")}
    chosen, lic_skipped = select(cfg, tier, key, session, limit, bad)
    new = cached = failed = 0
    state_dirty = False
    new_rows = []
    for qd, rec in chosen:
        sid = str(rec["id"])
        st = state.get(sid)
        if st and (ROOT / st["file"]).exists():
            cached += 1
            continue
        if dry_run:
            new += 1
            continue
        try:
            time.sleep(DELAY_S)
            path = download(session, rec)
        except Exception as e:
            failed += 1
            log.exception("download failed XC%s: %s", sid, e)
            code = getattr(getattr(e, "response", None), "status_code", None)
            if isinstance(e, ValueError) or code in (403, 404, 410):
                state[sid] = {"failed": f"{type(e).__name__}: {e}"[:200]}
                state_dirty = True
            continue
        rel = path.relative_to(ROOT).as_posix()
        row = {"source": "xc", "source_id": sid, "tier": tier, "query_id": qd["id"],
               "role_hint": qd["role_hint"], "ultrasonic": bool(qd.get("ultrasonic")),
               "recordist": rec.get("rec", ""), "licence": rec["_lic"],
               "licence_url": rec.get("lic", ""), "source_url": rec.get("url", ""),
               "species_sci": f"{rec.get('gen', '')} {rec.get('sp', '')}".strip(),
               "species_en": rec.get("en", ""), "grp": rec.get("grp", ""),
               "call_type": rec.get("type", ""), "quality": rec.get("q", ""),
               "smp": rec["_smp"], "length_s": rec["_len"], "also": rec.get("also", ""),
               "file": rel}
        state[sid] = {"file": rel}
        new_rows.append(row)
        new += 1
    if not dry_run and new_rows:
        RAW_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        fresh = not RAW_MANIFEST.exists()
        with RAW_MANIFEST.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=MANIFEST_COLS)
            if fresh:
                w.writeheader()
            w.writerows(new_rows)
    if not dry_run and (new_rows or state_dirty):
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1))
    tag = " [dry-run]" if dry_run else ""
    print(f"fetch_xc: {new} new, {cached} cached, {lic_skipped} skipped (licence), {failed} failed, {len(bad)} known-bad skipped{tag}")


def print_table(rows):
    cols = ["id", "tier", "num_recordings", "num_species", "note"]
    w = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.ljust(w[c]) for c in cols))
    print("  ".join("-" * w[c] for c in cols))
    for r in rows:
        print("  ".join(str(r[c]).ljust(w[c]) for c in cols))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="count-only pass, no downloads")
    ap.add_argument("--tier", choices=["clean", "sa", "both"], default="both")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    (ROOT / "data" / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=ROOT / "data" / "logs" / "fetch_xc.log",
                        level=logging.INFO, format="%(asctime)s %(message)s")
    if not args.probe and args.tier == "both":
        sys.exit("fetch_xc: downloads need an explicit --tier clean|sa")

    load_dotenv(ROOT / ".env")
    key = os.environ.get("XC_API_KEY")
    if (not key or "paste" in key.lower()) and not args.dry_run:
        sys.exit("fetch_xc: XC_API_KEY missing or still a placeholder in .env")

    cfg = yaml.safe_load((ROOT / "config" / "targets.yaml").read_text())["xeno_canto"]
    if not args.probe:
        run_fetch(cfg, args.tier, args.limit, args.dry_run, key)
        return
    tiers = ["clean", "sa"] if args.tier == "both" else [args.tier]
    rows, failed = probe(cfg, tiers, args.limit, args.dry_run, key)

    print_table(rows)
    if not args.dry_run:
        out = ROOT / "data" / "probe_report.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    zero = sum(1 for r in rows if r["note"] == "ZERO")
    print(f"fetch_xc --probe: {len(rows)} queries, {zero} zero, {failed} failed")


if __name__ == "__main__":
    main()
