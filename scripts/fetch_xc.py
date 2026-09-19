"""Xeno-canto fetcher. Currently implements --probe only (count-only, no audio)."""
import argparse
import csv
import logging
import os
import sys
import time
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
API = "https://xeno-canto.org/api/3/recordings"
USER_AGENT = "animal-noise-catalogue/0.1 (personal sample-library research; contact: wonderburg7@gmail.com)"
DELAY_S = 1.0

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
    if not args.probe:
        sys.exit("fetch_xc: only --probe is implemented")

    load_dotenv(ROOT / ".env")
    key = os.environ.get("XC_API_KEY")
    if (not key or "paste" in key.lower()) and not args.dry_run:
        sys.exit("fetch_xc: XC_API_KEY missing or still a placeholder in .env")

    cfg = yaml.safe_load((ROOT / "config" / "targets.yaml").read_text())["xeno_canto"]
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
