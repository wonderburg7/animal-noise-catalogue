"""Slice raw recordings into one-shots. Rows are appended to data/manifest.csv.

Two kinds of slice per recording:
  short  onset-segmented one-shots            (rules.yaml: slice)
  long   whole phrases, split on silence      (rules.yaml: slice_long)

Ultrasonic files (native sr > ultrasonic.detect_smp_above) are never resampled:
the header is reinterpreted so the samples play back slower and lower.
"""
import argparse
import csv
import json
import logging
import re
import unicodedata
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
    "kind", "quality", "file", "slice_path", "ultrasonic", "dur_s", "peak_db", "rms_db",
    "centroid_hz", "f0_hz", "f0_conf", "midi_note", "cents_off", "tonal_ratio",
    "onset_sharpness", "snr_db", "status", "reject_reason",
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


def finish(y, sr, s, e, p, frames_db, rejects):
    """Gate, pad, fade and normalise y[s:e]. Return (start_s, audio, snr, rms) or None."""
    pad = int(p["pad_s"] * sr)
    fade = int(p["fade_ms"] / 1000 * sr)
    win = int(p["snr_window_s"] * sr)
    w = frames_db[max(0, (s - win) // HOP_RMS): (e + win) // HOP_RMS + 1]
    snr = float(np.percentile(w, 90) - np.percentile(w, 10)) if len(w) else 0.0
    if snr < p["min_snr_db"]:
        rejects["low_snr"] += 1
        return None
    audio = y[max(0, s - pad): min(len(y), e + pad)].copy()
    if np.mean(np.abs(audio) >= 0.999) > p["max_clip_ratio"]:
        rejects["clipped"] += 1
        return None
    if float(np.max(np.abs(audio))) <= 0:
        rejects["silent"] += 1
        return None
    n = min(fade, len(audio) // 4)
    if n > 0:
        ramp = np.linspace(0, 1, n, dtype=np.float32)
        audio[:n] *= ramp
        audio[-n:] *= ramp[::-1]
    audio *= 10 ** (p["peak_dbfs"] / 20) / float(np.max(np.abs(audio)))
    return s / sr, audio, snr, float(np.sqrt(np.mean(audio ** 2)))


def cap_per_file(cands, limit, rejects):
    if len(cands) <= limit:
        return cands
    rejects["over_file_cap"] += len(cands) - limit
    return sorted(sorted(cands, key=lambda c: -c[3])[:limit], key=lambda c: c[0])


def slice_short(y, sr, p):
    """Onset-segmented one-shots. Return (kept slices, reject Counter)."""
    rejects = Counter()
    o = p["onset"]
    onsets = librosa.onset.onset_detect(y=y, sr=sr, hop_length=o["hop_length"],
                                        backtrack=o["backtrack"], delta=o["delta"],
                                        wait=o["wait"], units="samples")
    bounds = list(onsets) + [len(y)] if len(onsets) else [0, len(y)]
    frames_db = frame_rms_db(y)
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
        c = finish(y, sr, s, e, p, frames_db, rejects)
        if c:
            cands.append(c)
    return cap_per_file(cands, p["max_slices_per_file"], rejects), rejects


def slice_long(y, sr, p, lp):
    """Whole phrases: split on silence, merge close intervals, keep >= min_dur_s."""
    rejects = Counter()
    frames_db = frame_rms_db(y)
    merged = []
    for s, e in librosa.effects.split(y, top_db=lp["split_top_db"],
                                      frame_length=FRAME, hop_length=HOP_RMS):
        if merged and (s - merged[-1][1]) / sr < lp["merge_gap_s"]:
            merged[-1][1] = e
        else:
            merged.append([int(s), int(e)])
    cands = []
    for s, e in merged:
        if (e - s) / sr < lp["min_dur_s"]:
            rejects["too_short"] += 1
            continue
        e = min(e, s + int(lp["max_dur_s"] * sr))
        c = finish(y, sr, s, e, p, frames_db, rejects)
        if c:
            cands.append(c)
    return cap_per_file(cands, lp["max_slices_per_file"], rejects), rejects


def ensure_columns(quality_by_id):
    """Migrate an older manifest to the current column set (kind, quality)."""
    if not MANIFEST.exists():
        return
    with MANIFEST.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        have, rows = reader.fieldnames, list(reader)
    if have == MANIFEST_COLS:
        return
    for r in rows:
        r.setdefault("kind", "short")
        r.setdefault("quality", quality_by_id.get(r["source_id"], ""))
    with MANIFEST.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLS)
        w.writeheader()
        w.writerows(rows)


def slug(text):
    """'Dendrocopos major' -> 'dendrocopos-major' (lowercase ASCII, hyphens)."""
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "unknown"


def slice_rel(species_sci, slice_id):
    return f"data/slices/{slug(species_sci)}_{slice_id}.wav"


def rename_existing():
    """Give already-written slice files species names. Idempotent."""
    if not MANIFEST.exists():
        return 0
    with MANIFEST.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        cols, rows = reader.fieldnames, list(reader)
    moved = 0
    for r in rows:
        want = slice_rel(r["species_sci"], r["slice_id"])
        if r["slice_path"] != want and (ROOT / r["slice_path"]).exists():
            (ROOT / r["slice_path"]).replace(ROOT / want)
            r["slice_path"] = want
            moved += 1
    if moved:
        tmp = MANIFEST.with_suffix(".csv.tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        tmp.replace(MANIFEST)
    return moved


def make_row(r, kind, slice_id, rel, ultra, audio, sr_out, snr, rms):
    return {
        "slice_id": slice_id, "tier": r["tier"], "source": r["source"],
        "source_id": r["source_id"], "source_url": r["source_url"],
        "recordist": r["recordist"], "licence": r["licence"],
        "licence_url": r["licence_url"], "species_sci": r["species_sci"],
        "species_en": r["species_en"], "grp": r["grp"], "call_type": r["call_type"],
        "role_hint": r["role_hint"], "kind": kind, "quality": r["quality"],
        "slice_path": rel, "ultrasonic": ultra, "dur_s": round(len(audio) / sr_out, 4),
        "peak_db": round(db(float(np.max(np.abs(audio)))), 2),
        "rms_db": round(db(rms), 2), "snr_db": round(snr, 2), "status": "pending",
    }


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
    lp = cfg.get("slice_long", {})
    do_long = bool(lp.get("enabled"))
    raws = list(csv.DictReader(RAW_MANIFEST.open(encoding="utf-8")))
    if not args.dry_run:
        ensure_columns({r["source_id"]: r["quality"] for r in raws})
        rename_existing()
    if args.tier != "both":
        raws = [r for r in raws if r["tier"] == args.tier]
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    files = cached = failed = 0
    counts, rejects, rows = Counter(), Counter(), []
    for r in raws:
        sid = r["source_id"]
        st = state.get(sid, {})
        need_short = sid not in state
        need_long = do_long and "long" not in st
        if not (need_short or need_long):
            cached += 1
            continue
        if args.limit and files >= args.limit:
            break
        path = ROOT / r["file"]
        try:
            y, sr = load_audio(path)
            y, sr_out, p, ultra = prepare(y, sr, cfg)
            jobs = []
            if need_short:
                jobs.append(("short", *slice_short(y, sr_out, p)))
            if need_long:
                jobs.append(("long", *slice_long(y, sr_out, p, lp)))
        except Exception:
            failed += 1
            log.exception("slice failed %s", path)
            continue
        files += 1
        for kind, kept, rej in jobs:
            rejects.update({f"{kind}:{k}": v for k, v in rej.items()})
            log.info("%s %s: sr=%s ultra=%s kept=%d rejected=%s", sid, kind, sr, ultra,
                     len(kept), dict(rej))
            width = max(2, len(str(len(kept))))
            prefix = "L" if kind == "long" else ""
            for n, (_, audio, snr, rms) in enumerate(kept, 1):
                slice_id = f"xc{sid}_{prefix}{n:0{width}d}"
                rel = slice_rel(r["species_sci"], slice_id)
                if not args.dry_run:
                    SLICE_DIR.mkdir(parents=True, exist_ok=True)
                    sf.write(ROOT / rel, audio, sr_out, subtype="PCM_24")
                rows.append(make_row(r, kind, slice_id, rel, ultra, audio, sr_out, snr, rms))
            counts[kind] += len(kept)
            entry = state.setdefault(sid, {})
            if kind == "short":
                entry.update(slices=len(kept), rejected=dict(rej))
            else:
                entry["long"] = {"slices": len(kept), "rejected": dict(rej)}

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
    print(f"slice: {files} files, {counts['short']} short + {counts['long']} long slices, "
          f"{cached} cached, {failed} failed, rejected: {rej_txt}{tag}")


if __name__ == "__main__":
    main()
