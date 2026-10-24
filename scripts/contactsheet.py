"""Build review contact sheets: review/sheet_NNN.png (8x8 mel grid) and
review/sheet_NNN.wav (the same slices in order, a 40 ms click between them).

Sheet membership is frozen in review/sheets.json the first time a slice is
placed, so sheet-local indices stay valid for curate.py. Sheets never mix
tiers. Only slices matching --filter and not yet on a sheet are added.
"""
import argparse
import csv
import json
import logging
import re
import warnings
from pathlib import Path

import librosa
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "manifest.csv"
REVIEW = ROOT / "review"
SHEETS = REVIEW / "sheets.json"
WAV_SR = 44100
GRID = 8

log = logging.getLogger("contactsheet")


def make_filter(expr):
    m = re.fullmatch(r"\s*(\w+)\s*(==|!=)\s*(.*?)\s*", expr)
    if not m:
        raise SystemExit(f"contactsheet: bad --filter {expr!r}, expected col==value")
    col, op, val = m.groups()
    return lambda r: (r.get(col, "") == val) == (op == "==")


def bell(sr, freqs, level=0.12, note_s=0.30, step_s=0.22):
    """Soft sine bell(s): 15 ms attack, exponential decay. One freq per note."""
    total = int((step_s * (len(freqs) - 1) + note_s) * sr)
    out = np.zeros(total, dtype=np.float32)
    t = np.arange(int(note_s * sr)) / sr
    env = np.minimum(1, t / 0.015) * np.exp(-t * 9)
    for k, f in enumerate(freqs):
        a = int(k * step_s * sr)
        out[a:a + len(t)] += (level * np.sin(2 * np.pi * f * t) * env).astype(np.float32)
    return out


def markers(sr):
    """Every slice: single soft 660 Hz bell. Every fifth: rising two-note chime."""
    return bell(sr, [660]), bell(sr, [523, 784])


def render_png(items, path, title):
    fig, axes = plt.subplots(GRID, GRID, figsize=(16, 16.8), dpi=80)
    for ax in axes.ravel():
        ax.axis("off")
    for ax, (i, row, y, sr) in zip(axes.ravel(), items):
        mel = librosa.power_to_db(
            librosa.feature.melspectrogram(y=y, sr=sr, n_mels=64, fmax=sr / 2), ref=np.max)
        ax.imshow(mel, origin="lower", aspect="auto", cmap="magma", vmin=-70, vmax=0)
        ax.set_title(f"{i}  {float(row['dur_s']):.2f}s  {row['role'] or '-'}  {row.get('quality', '')}",
                     fontsize=9, pad=2)
    fig.suptitle(title, fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.98))
    fig.savefig(path)
    plt.close(fig)


def render_wav(items, path):
    """slice, marker, 1 s silence; the marker after every 5th slice is the chime."""
    parts = []
    plain, fifth = markers(WAV_SR)
    silence = np.zeros(WAV_SR, dtype=np.float32)
    for i, _, y, sr in items:
        if sr != WAV_SR:
            y = librosa.resample(y, orig_sr=sr, target_sr=WAV_SR, res_type="soxr_hq")
        parts += [y.astype(np.float32), np.zeros(int(0.15 * WAV_SR), dtype=np.float32),
                  fifth if i % 5 == 0 else plain, silence]
    sf.write(path, np.concatenate(parts), WAV_SR, subtype="PCM_16")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--batch-long", type=int, default=24,
                    help="slices per sheet for long slices (they take longer to hear)")
    ap.add_argument("--filter", default="status==pending")
    ap.add_argument("--limit", type=int, default=0, help="max new sheets")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rerender-wav", action="store_true",
                    help="rebuild the WAV of every existing sheet, membership unchanged")
    args = ap.parse_args()
    if args.batch > GRID * GRID:
        raise SystemExit(f"contactsheet: --batch cannot exceed {GRID * GRID}")

    (ROOT / "data" / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=ROOT / "data" / "logs" / "contactsheet.log",
                        level=logging.INFO, format="%(asctime)s %(message)s")
    warnings.filterwarnings("ignore")
    keep = make_filter(args.filter)

    rows = list(csv.DictReader(MANIFEST.open(encoding="utf-8")))
    sheets = json.loads(SHEETS.read_text()) if SHEETS.exists() else {}
    if args.rerender_wav:
        by_id = {r["slice_id"]: r for r in rows}
        names = list(sheets)[:args.limit] if args.limit else list(sheets)
        for name in names:
            if args.dry_run:
                continue
            items = []
            for i, sid in enumerate(sheets[name], 1):
                y, sr = sf.read(ROOT / by_id[sid]["slice_path"], dtype="float32")
                items.append((i, by_id[sid], y, sr))
            render_wav(items, REVIEW / f"{name}.wav")
        tag = " [dry-run]" if args.dry_run else ""
        print(f"contactsheet: re-rendered {len(names)} wavs{tag}")
        return
    placed = {sid for ids in sheets.values() for sid in ids}
    todo = [r for r in rows if keep(r) and r["slice_id"] not in placed and r["role"]]
    todo.sort(key=lambda r: (r["tier"], r.get("kind") or "short", r["role"], r["slice_id"]))

    new_sheets, n_slices = 0, 0
    for tier, kind in [(t, k) for t in ("clean", "sa") for k in ("short", "long")]:
        group = [r for r in todo if r["tier"] == tier and (r.get("kind") or "short") == kind]
        size = args.batch_long if kind == "long" else args.batch
        for k in range(0, len(group), size):
            if args.limit and new_sheets >= args.limit:
                break
            batch = group[k:k + size]
            name = f"sheet_{len(sheets) + 1:03d}"
            new_sheets += 1
            n_slices += len(batch)
            if args.dry_run:
                continue
            items = []
            for i, r in enumerate(batch, 1):
                y, sr = sf.read(ROOT / r["slice_path"], dtype="float32")
                items.append((i, r, y, sr))
            REVIEW.mkdir(exist_ok=True)
            render_png(items, REVIEW / f"{name}.png",
                       f"{name}  tier={tier}  {kind}  {len(batch)} slices")
            render_wav(items, REVIEW / f"{name}.wav")
            sheets[name] = [r["slice_id"] for r in batch]
            SHEETS.write_text(json.dumps(sheets, indent=1))
            log.info("%s: %d slices, tier %s", name, len(batch), tier)
    tag = " [dry-run]" if args.dry_run else ""
    print(f"contactsheet: {new_sheets} new sheets, {n_slices} slices, {len(sheets)} sheets total{tag}")


if __name__ == "__main__":
    main()
