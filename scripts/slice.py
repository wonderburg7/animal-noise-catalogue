"""Slice raw recordings into one-shots. Rows are appended to data/manifest.csv.

Two kinds of slice per recording:
  short  onset-segmented one-shots            (rules.yaml: slice)
  long   whole phrases, split on silence      (rules.yaml: slice_long)

Ultrasonic files (native sr > ultrasonic.detect_smp_above) are never resampled:
the header is reinterpreted so the samples play back slower and lower.

start_s / end_s place each slice on the prepared timeline of its source (after
resample and high-pass; for ultrasonic files, the slowed playback timeline).
`--backfill-times` fills them for rows sliced before they were recorded.
"""
import argparse
import csv
import json
import logging
from collections import Counter
from pathlib import Path

import librosa
import numpy as np
import scipy.signal
import soundfile as sf
import yaml

from naming import slug

ROOT = Path(__file__).resolve().parent.parent
RAW_MANIFEST = ROOT / "data" / "raw_manifest.csv"
MANIFEST = ROOT / "data" / "manifest.csv"
SLICE_DIR = ROOT / "data" / "slices"
STATE = ROOT / "data" / "state" / "slice.json"

MANIFEST_COLS = [
    "slice_id", "tier", "source", "source_id", "source_url", "recordist", "licence",
    "licence_url", "species_sci", "species_en", "grp", "call_type", "role_hint", "role",
    "kind", "quality", "file", "slice_path", "ultrasonic", "start_s", "end_s", "edited",
    "dur_s", "peak_db", "rms_db",
    "centroid_hz", "f0_hz", "f0_conf", "midi_note", "cents_off", "tonal_ratio",
    "onset_sharpness", "snr_db", "status", "reject_reason",
]
FRAME, HOP_RMS = 2048, 512
FEATURE_COLS = ["centroid_hz", "f0_hz", "f0_conf", "midi_note", "cents_off", "tonal_ratio",
                "onset_sharpness"]

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


def shape(audio, sr, p):
    """Fade in/out and peak-normalise in place. Same processing for every cut."""
    fade = int(p["fade_ms"] / 1000 * sr)
    n = min(fade, len(audio) // 4)
    if n > 0:
        ramp = np.linspace(0, 1, n, dtype=np.float32)
        audio[:n] *= ramp
        audio[-n:] *= ramp[::-1]
    audio *= 10 ** (p["peak_dbfs"] / 20) / float(np.max(np.abs(audio)))
    return audio


def snr_at(frames_db, s, e, sr, p):
    """90th minus 10th percentile frame RMS (dB) over y[s:e] plus context either side."""
    win = int(p["snr_window_s"] * sr)
    w = frames_db[max(0, (s - win) // HOP_RMS): (e + win) // HOP_RMS + 1]
    return float(np.percentile(w, 90) - np.percentile(w, 10)) if len(w) else 0.0


def finish(y, sr, s, e, p, frames_db, rejects):
    """Gate, pad, fade and normalise y[s:e].

    Return (onset_s, audio, snr, rms, (start_s, end_s)) or None, where start_s/end_s
    are the padded bounds actually cut.
    """
    pad = int(p["pad_s"] * sr)
    snr = snr_at(frames_db, s, e, sr, p)
    if snr < p["min_snr_db"]:
        rejects["low_snr"] += 1
        return None
    a, b = max(0, s - pad), min(len(y), e + pad)
    audio = y[a:b].copy()
    if np.mean(np.abs(audio) >= 0.999) > p["max_clip_ratio"]:
        rejects["clipped"] += 1
        return None
    if float(np.max(np.abs(audio))) <= 0:
        rejects["silent"] += 1
        return None
    shape(audio, sr, p)
    return s / sr, audio, snr, float(np.sqrt(np.mean(audio ** 2))), (a / sr, b / sr)


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
        r.setdefault("edited", "False")
    with MANIFEST.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLS)
        w.writeheader()
        w.writerows(rows)


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


def read_manifest():
    with MANIFEST.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def write_manifest(cols, rows):
    tmp = MANIFEST.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(MANIFEST)


def cut_source(r, cfg, kinds):
    """Re-run slicing on one raw recording. Yield (kind, slice_id, audio, sr_out, bounds)."""
    y, sr = load_audio(ROOT / r["file"])
    y, sr_out, p, _ = prepare(y, sr, cfg)
    for kind in kinds:
        kept = (slice_short(y, sr_out, p) if kind == "short"
                else slice_long(y, sr_out, p, cfg["slice_long"]))[0]
        width = max(2, len(str(len(kept))))
        prefix = "L" if kind == "long" else ""
        for n, (_, audio, _, _, bounds) in enumerate(kept, 1):
            yield kind, f"xc{r['source_id']}_{prefix}{n:0{width}d}", audio, sr_out, bounds


def backfill_times(cfg, tier, limit, dry_run):
    """Fill start_s/end_s for rows sliced before they were recorded. Matches by slice id
    and accepts only when the re-cut duration equals the stored dur_s."""
    raws = {r["source_id"]: r for r in csv.DictReader(RAW_MANIFEST.open(encoding="utf-8"))}
    if not dry_run:
        ensure_columns({k: r["quality"] for k, r in raws.items()})
    cols, rows = read_manifest()
    todo = {}
    for row in rows:
        if row.get("start_s") or row.get("edited") == "True":
            continue
        if tier != "both" and row["tier"] != tier:
            continue
        todo.setdefault(row["source_id"], {})[row["slice_id"]] = row
    files = matched = unmatched = failed = 0
    for sid, want in todo.items():
        if limit and files >= limit:
            break
        files += 1
        kinds = sorted({w["kind"] for w in want.values()}, reverse=True)  # short, long
        try:
            for kind, slice_id, audio, sr_out, (t0, t1) in cut_source(raws[sid], cfg, kinds):
                row = want.pop(slice_id, None)
                if row is None:
                    continue
                if round(len(audio) / sr_out, 4) != float(row["dur_s"]):
                    unmatched += 1
                    log.info("backfill %s: dur %s != %s", slice_id,
                             round(len(audio) / sr_out, 4), row["dur_s"])
                    continue
                row["start_s"], row["end_s"] = round(t0, 4), round(t1, 4)
                matched += 1
        except Exception:
            failed += 1
            log.exception("backfill failed %s", sid)
            continue
        unmatched += len(want)
        for slice_id in want:
            log.info("backfill %s: no slice with that id on re-cut", slice_id)
    if not dry_run and matched:
        write_manifest(cols, rows)
    tag = " [dry-run]" if dry_run else ""
    print(f"slice --backfill-times: {files} files, {matched} matched, {unmatched} unmatched, "
          f"{failed} failed{tag}")


def make_row(r, kind, slice_id, rel, ultra, audio, sr_out, snr, rms, bounds):
    return {
        "slice_id": slice_id, "tier": r["tier"], "source": r["source"],
        "source_id": r["source_id"], "source_url": r["source_url"],
        "recordist": r["recordist"], "licence": r["licence"],
        "licence_url": r["licence_url"], "species_sci": r["species_sci"],
        "species_en": r["species_en"], "grp": r["grp"], "call_type": r["call_type"],
        "role_hint": r["role_hint"], "kind": kind, "quality": r["quality"],
        "slice_path": rel, "ultrasonic": ultra, "start_s": round(bounds[0], 4),
        "end_s": round(bounds[1], 4), "edited": False, "dur_s": round(len(audio) / sr_out, 4),
        "peak_db": round(db(float(np.max(np.abs(audio)))), 2),
        "rms_db": round(db(rms), 2), "snr_db": round(snr, 2), "status": "pending",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["clean", "sa", "both"], default="both")
    ap.add_argument("--limit", type=int, default=0, help="max raw files to process")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backfill-times", action="store_true",
                    help="fill start_s/end_s on existing rows; slices nothing new")
    args = ap.parse_args()

    (ROOT / "data" / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=ROOT / "data" / "logs" / "slice.log",
                        level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = yaml.safe_load((ROOT / "config" / "rules.yaml").read_text())
    if args.backfill_times:
        backfill_times(cfg, args.tier, args.limit, args.dry_run)
        return
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
            for n, (_, audio, snr, rms, bounds) in enumerate(kept, 1):
                slice_id = f"xc{sid}_{prefix}{n:0{width}d}"
                rel = slice_rel(r["species_sci"], slice_id)
                if not args.dry_run:
                    SLICE_DIR.mkdir(parents=True, exist_ok=True)
                    sf.write(ROOT / rel, audio, sr_out, subtype="PCM_24")
                rows.append(make_row(r, kind, slice_id, rel, ultra, audio, sr_out, snr, rms,
                                    bounds))
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
