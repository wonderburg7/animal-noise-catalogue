"""Slice raw recordings into one-shots. Rows are appended to data/manifest.csv.

Ultrasonic files (native sr > ultrasonic.detect_smp_above) are never resampled:
the header is reinterpreted so the samples play back slower and lower.
"""
import argparse
import csv
import json
import logging
import sys
from collections import Counter
from pathlib import Path

import librosa
import numpy as np
import scipy.signal
import soundfile as sf
import yaml

ROOT = Path(__file__).resolve().parent.parent
RAW_MANIFEST = ROOT / "data" / "raw_manifest.csv"
MANIFEST = ROOT / "data" / "manifest.csv"
SLICE_DIR = ROOT / "data" / "slices"
STATE = ROOT / "data" / "state" / "slice.json"

MANIFEST_COLS = [
    "slice_id", "tier", "source", "source_id", "source_url", "recordist", "licence",
    "licence_url", "species_sci", "species_en", "grp", "call_type", "role_hint", "role",
    "file", "slice_path", "ultrasonic", "dur_s", "peak_db", "rms_db", "centroid_hz",
    "f0_hz", "f0_conf", "midi_note", "cents_off", "tonal_ratio", "onset_sharpness",
    "snr_db", "status", "reject_reason",
]
FRAME, HOP_RMS = 2048, 512

log = logging.getLogger("slice")


def load_audio(path):
    try:
        y, sr = sf.read(path, dtype="float32", always_2d=False)
        if y.ndim > 1:
            y = y.mean(axis=1)
    except Exception:
        y, sr = librosa.load(path, sr=None, mono=True)
    return y.astype(np.float32), int(sr)


def frame_rms_db(y):
    rms = librosa.feature.rms(y=y, frame_length=FRAME, hop_length=HOP_RMS)[0]
    return 20 * np.log10(np.maximum(rms, 1e-9))


def db(x):
    return float(20 * np.log10(max(x, 1e-9)))


def prepare(y, sr, cfg):
    """Return (y, sr_out, params, ultrasonic). Ultrasonic keeps samples, relabels sr."""
    u = cfg["ultrasonic"]
    p = dict(cfg["slice"])
    if sr > u["detect_smp_above"]:
        y = y[: int(u["truncate_source_s"] * sr)]  # truncate BEFORE slowing
        p.update(highpass_hz=u["highpass_hz"], max_slices_per_file=u["max_slices_per_file"],
                 min_snr_db=u["min_snr_db"])
        return y, u["target_playback_sr"], p, True
    target = p["target_sr"]
    if sr != target:
        y = librosa.resample(y, orig_sr=sr, target_sr=target, res_type="soxr_hq")
    if p["highpass_hz"] > 0:
        sos = scipy.signal.butter(4, p["highpass_hz"], btype="highpass", fs=target, output="sos")
        y = scipy.signal.sosfiltfilt(sos, y).astype(np.float32)
    return y, target, p, False


def slice_file(y, sr, p):
    """Return (kept slices [(start_s, audio, snr, rms)], reject Counter)."""
    rejects = Counter()
    o = p["onset"]
    onsets = librosa.onset.onset_detect(y=y, sr=sr, hop_length=o["hop_length"],
                                        backtrack=o["backtrack"], delta=o["delta"],
                                        wait=o["wait"], units="samples")
    bounds = list(onsets) + [len(y)]
    if not len(onsets):
        bounds = [0, len(y)]
    frames_db = frame_rms_db(y)
    pad = int(p["pad_s"] * sr)
    fade = int(p["fade_ms"] / 1000 * sr)
    win = int(p["snr_window_s"] * sr)
    cands = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        seg = y[a:b]
        if len(seg) == 0:
            continue
        _, (i0, i1) = librosa.effects.trim(seg, top_db=p["trim_top_db"],
                                           frame_length=FRAME // 4, hop_length=HOP_RMS // 4)
        s, e = a + i0, a + i1
        if (e - s) / sr < p["min_dur_s"]:
            rejects["too_short"] += 1
            continue
        if (e - s) / sr > p["max_dur_s"]:
            if p["over_max_action"] == "reject":
                rejects["too_long"] += 1
                continue
            e = s + int(p["max_dur_s"] * sr)
        w = frames_db[max(0, (s - win) // HOP_RMS): (e + win) // HOP_RMS + 1]
        snr = float(np.percentile(w, 90) - np.percentile(w, 10)) if len(w) else 0.0
        if snr < p["min_snr_db"]:
            rejects["low_snr"] += 1
            continue
        audio = y[max(0, s - pad): min(len(y), e + pad)].copy()
        if np.mean(np.abs(audio) >= 0.999) > p["max_clip_ratio"]:
            rejects["clipped"] += 1
            continue
        peak = float(np.max(np.abs(audio)))
        if peak <= 0:
            rejects["silent"] += 1
            continue
        n = min(fade, len(audio) // 4)
        if n > 0:
            ramp = np.linspace(0, 1, n, dtype=np.float32)
            audio[:n] *= ramp
            audio[-n:] *= ramp[::-1]
        audio *= 10 ** (p["peak_dbfs"] / 20) / float(np.max(np.abs(audio)))
        cands.append((s / sr, audio, snr, float(np.sqrt(np.mean(audio ** 2)))))
    if len(cands) > p["max_slices_per_file"]:
        rejects["over_file_cap"] += len(cands) - p["max_slices_per_file"]
        keep = sorted(sorted(cands, key=lambda c: -c[3])[: p["max_slices_per_file"]],
                      key=lambda c: c[0])
        cands = keep
    return cands, rejects


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["clean", "sa", "both"], default="both")
    ap.add_argument("--limit", type=int, default=0, help="max raw files to process")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    (ROOT / "data" / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=ROOT / "data" / "logs" / "slice.log",
                        level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = yaml.safe_load((ROOT / "config" / "rules.yaml").read_text())
    raws = list(csv.DictReader(RAW_MANIFEST.open(encoding="utf-8")))
    if args.tier != "both":
        raws = [r for r in raws if r["tier"] == args.tier]
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    files = slices_n = cached = failed = 0
    rejects, rows = Counter(), []
    for r in raws:
        sid = r["source_id"]
        if sid in state:
            cached += 1
            continue
        if args.limit and files >= args.limit:
            break
        path = ROOT / r["file"]
        try:
            y, sr = load_audio(path)
            y, sr_out, p, ultra = prepare(y, sr, cfg)
            kept, rej = slice_file(y, sr_out, p)
        except Exception:
            failed += 1
            log.exception("slice failed %s", path)
            continue
        files += 1
        rejects.update(rej)
        log.info("%s: sr=%s ultra=%s kept=%d rejected=%s", sid, sr, ultra, len(kept), dict(rej))
        width = max(2, len(str(len(kept))))
        for n, (start, audio, snr, rms) in enumerate(kept, 1):
            slice_id = f"xc{sid}_{n:0{width}d}"
            rel = f"data/slices/{slice_id}.wav"
            if not args.dry_run:
                SLICE_DIR.mkdir(parents=True, exist_ok=True)
                sf.write(ROOT / rel, audio, sr_out, subtype="PCM_24")
            rows.append({
                "slice_id": slice_id, "tier": r["tier"], "source": r["source"],
                "source_id": sid, "source_url": r["source_url"], "recordist": r["recordist"],
                "licence": r["licence"], "licence_url": r["licence_url"],
                "species_sci": r["species_sci"], "species_en": r["species_en"],
                "grp": r["grp"], "call_type": r["call_type"], "role_hint": r["role_hint"],
                "slice_path": rel, "ultrasonic": ultra,
                "dur_s": round(len(audio) / sr_out, 4),
                "peak_db": round(db(float(np.max(np.abs(audio)))), 2),
                "rms_db": round(db(rms), 2), "snr_db": round(snr, 2), "status": "pending",
            })
        slices_n += len(kept)
        state[sid] = {"slices": len(kept), "rejected": dict(rej)}

    if not args.dry_run and files:
        fresh = not MANIFEST.exists()
        with MANIFEST.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=MANIFEST_COLS)
            if fresh:
                w.writeheader()
            w.writerows(rows)
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1))
    tag = " [dry-run]" if args.dry_run else ""
    rej_txt = ", ".join(f"{k} {v}" for k, v in sorted(rejects.items())) or "none"
    print(f"slice: {files} files, {slices_n} slices, {cached} cached, {failed} failed, "
          f"rejected: {rej_txt}{tag}")


if __name__ == "__main__":
    main()
