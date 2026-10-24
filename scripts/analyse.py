"""Fill the feature columns of data/manifest.csv for slices that have none yet.

Definitions (docs/build-spec.md section 6):
  tonal_ratio     harmonic RMS / (harmonic + percussive RMS), librosa hpss
  onset_sharpness 1 - (time from start to peak amplitude / duration)
  f0_hz, f0_conf  librosa.pyin fmin=80 fmax=12000; f0 = median of voiced frames,
                  f0_conf = mean voiced probability over those frames
  midi_note, cents_off  only when f0_conf >= classify.tonal.min_f0_conf
"""
import argparse
import csv
import logging
import warnings
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "manifest.csv"

log = logging.getLogger("analyse")


def features(y, sr, min_conf):
    n = len(y)
    peak_i = int(np.argmax(np.abs(y)))
    out = {"onset_sharpness": round(1 - peak_i / max(n - 1, 1), 4)}
    out["centroid_hz"] = round(float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr))), 1)

    harm, perc = librosa.effects.hpss(y)
    h, p = float(np.sqrt(np.mean(harm ** 2))), float(np.sqrt(np.mean(perc ** 2)))
    out["tonal_ratio"] = round(h / (h + p), 4) if h + p > 0 else 0.0

    f0, voiced, prob = librosa.pyin(y, fmin=80, fmax=12000, sr=sr)
    ok = voiced & np.isfinite(f0)
    if ok.any():
        med = float(np.median(f0[ok]))
        conf = float(np.mean(prob[ok]))
        out.update(f0_hz=round(med, 2), f0_conf=round(conf, 4))
        if conf >= min_conf:
            midi = 69 + 12 * np.log2(med / 440.0)
            out.update(midi_note=int(round(midi)), cents_off=round(100 * (midi - round(midi)), 1))
    else:
        out["f0_conf"] = 0.0
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["clean", "sa", "both"], default="both")
    ap.add_argument("--limit", type=int, default=0, help="max slices to analyse")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    (ROOT / "data" / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=ROOT / "data" / "logs" / "analyse.log",
                        level=logging.INFO, format="%(asctime)s %(message)s")
    warnings.filterwarnings("ignore")
    rules = yaml.safe_load((ROOT / "config" / "rules.yaml").read_text())
    min_conf = rules["classify"]["tonal"]["min_f0_conf"]

    with MANIFEST.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        cols, rows = reader.fieldnames, list(reader)

    done = skipped = failed = 0
    for r in rows:
        if r["tonal_ratio"] != "":
            skipped += 1
            continue
        if args.tier != "both" and r["tier"] != args.tier:
            continue
        if args.limit and done >= args.limit:
            break
        try:
            y, sr = sf.read(ROOT / r["slice_path"], dtype="float32")
            feats = features(y, sr, min_conf)
        except Exception:
            failed += 1
            log.exception("analyse failed %s", r["slice_id"])
            continue
        if not args.dry_run:
            r.update({k: v for k, v in feats.items()})
        done += 1

    if not args.dry_run and done:
        tmp = MANIFEST.with_suffix(".csv.tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        tmp.replace(MANIFEST)
    tag = " [dry-run]" if args.dry_run else ""
    print(f"analyse: {done} analysed, {skipped} already done, {failed} failed{tag}")


if __name__ == "__main__":
    main()
