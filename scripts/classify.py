"""Assign `role` in data/manifest.csv from config/rules.yaml.

Pure function of the feature columns, so it is safe to re-run after any
threshold change. Precedence: perc, tonal, drone, then texture.
"""
import argparse
import csv
import logging
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "manifest.csv"

log = logging.getLogger("classify")


def num(row, key):
    v = row.get(key, "")
    return float(v) if v != "" else None


def classify(row, rules):
    dur, sharp = num(row, "dur_s"), num(row, "onset_sharpness")
    tonal, conf = num(row, "tonal_ratio"), num(row, "f0_conf")
    perc, ton, drone = rules["perc"], rules["tonal"], rules["drone"]
    if dur is None or sharp is None or tonal is None:
        return ""  # not analysed yet
    if dur < perc["max_dur_s"] and sharp >= perc["min_onset_sharpness"]:
        return "perc"
    if (tonal >= ton["min_tonal_ratio"] and (conf or 0) >= ton["min_f0_conf"]
            and ton["min_dur_s"] <= dur <= ton["max_dur_s"]):
        return "tonal"
    if dur >= drone["min_dur_s"] and tonal >= drone["min_tonal_ratio"]:
        return "drone"
    return "texture"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["clean", "sa", "both"], default="both")
    ap.add_argument("--limit", type=int, default=0, help="max rows to classify")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    (ROOT / "data" / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=ROOT / "data" / "logs" / "classify.log",
                        level=logging.INFO, format="%(asctime)s %(message)s")
    rules = yaml.safe_load((ROOT / "config" / "rules.yaml").read_text())["classify"]

    with MANIFEST.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        cols, rows = reader.fieldnames, list(reader)

    counts, changed, n = Counter(), 0, 0
    for r in rows:
        if args.tier != "both" and r["tier"] != args.tier:
            continue
        if args.limit and n >= args.limit:
            break
        n += 1
        role = classify(r, rules)
        counts[role or "unanalysed"] += 1
        if role != r["role"]:
            changed += 1
            if not args.dry_run:
                r["role"] = role

    if not args.dry_run and changed:
        tmp = MANIFEST.with_suffix(".csv.tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        tmp.replace(MANIFEST)
    tag = " [dry-run]" if args.dry_run else ""
    dist = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    print(f"classify: {n} rows, {changed} changed, {dist}{tag}")


if __name__ == "__main__":
    main()
