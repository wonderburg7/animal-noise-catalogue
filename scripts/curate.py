"""Apply review decisions from a contact sheet to data/manifest.csv.

  curate.py --sheet 7 --reject "3,7,12-15,40"   listed -> reject (manual),
                                                 other pending on the sheet -> keep
  curate.py --sheet 7 --restore "7"              listed -> keep, reject_reason cleared
  curate.py --reject "3,7"                       no --sheet: first sheet with pending slices

Indices are the 1-based numbers printed on the sheet. Idempotent; a slice
can be flipped back at any time with --restore or --reject.
"""
import argparse
import csv
import json
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "manifest.csv"
SHEETS = ROOT / "review" / "sheets.json"

log = logging.getLogger("curate")


def parse_indices(text, size):
    out = set()
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        lo, hi = int(a), int(b or a)
        if lo < 1 or hi > size or lo > hi:
            raise SystemExit(f"curate: index {part!r} outside 1-{size}")
        out.update(range(lo, hi + 1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", type=int, help="sheet number; default first with pending slices")
    ap.add_argument("--reject", default="", help='indices to reject, e.g. "3,7,12-15"')
    ap.add_argument("--restore", default="", help="indices to set back to keep")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="unused; accepted for contract")
    args = ap.parse_args()

    (ROOT / "data" / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=ROOT / "data" / "logs" / "curate.log",
                        level=logging.INFO, format="%(asctime)s %(message)s")
    sheets = json.loads(SHEETS.read_text())
    with MANIFEST.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        cols, rows = reader.fieldnames, list(reader)
    by_id = {r["slice_id"]: r for r in rows}

    if args.sheet is not None:
        name = f"sheet_{args.sheet:03d}"
    else:
        name = next((n for n, ids in sheets.items()
                     if any(by_id[i]["status"] == "pending" for i in ids)), None)
    if name not in sheets:
        raise SystemExit(f"curate: no such sheet {name}")
    ids = sheets[name]
    rej, res = parse_indices(args.reject, len(ids)), parse_indices(args.restore, len(ids))
    if rej & res:
        raise SystemExit("curate: an index cannot be in both --reject and --restore")

    rejected = restored = kept = 0
    for i, sid in enumerate(ids, 1):
        r = by_id[sid]
        if i in rej:
            new = ("reject", "manual")
        elif i in res:
            new = ("keep", "")
        elif r["status"] == "pending":
            new = ("keep", "")
        else:
            continue
        if (r["status"], r["reject_reason"]) != new:
            r["status"], r["reject_reason"] = new
            rejected += new[0] == "reject"
            restored += i in res
            kept += new[0] == "keep" and i not in res
    if not args.dry_run:
        tmp = MANIFEST.with_suffix(".csv.tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        tmp.replace(MANIFEST)
    tag = " [dry-run]" if args.dry_run else ""
    print(f"curate: {name}: {rejected} rejected, {kept} kept, {restored} restored{tag}")


if __name__ == "__main__":
    main()
