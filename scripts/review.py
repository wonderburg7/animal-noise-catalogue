"""Review app: one source recording at a time, as a waveform with its slices marked.

  python scripts/review.py                         pending slices, opens the browser
  python scripts/review.py --filter status==maybe  revisit the maybe pile
  python scripts/review.py --limit 2 --dry-run     two files, nothing written

Each decision is written to data/manifest.csv as it is made: keep, reject
(reject_reason=manual) or maybe. Edges dragged in the app re-cut the slice file
from the prepared source, set edited=True and blank the features so analyse.py
and classify.py refill them. Keys live in config/review_keys.yaml.
Finish in the app (or Ctrl+C in the terminal) stops the server and prints the
summary line.
"""
import argparse
import csv
import io
import json
import logging
import re
import threading
import warnings
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import numpy as np
import soundfile as sf
import yaml

import slice as slicer
from naming import slug

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent / "review_app"
CACHE = ROOT / "data" / "review"
KEYS = ROOT / "config" / "review_keys.yaml"
STATIC = {"index.html": "text/html", "app.js": "text/javascript", "app.css": "text/css"}
DEFAULT_KEYS = {"accept": "a", "decline": "d", "maybe": "s", "edit": "e", "prev": "ArrowLeft",
                "next": "ArrowRight", "next_file": "n", "replay": "Space",
                "play_file": "p", "cancel": "Escape"}
DECISIONS = {"keep": "", "reject": "manual", "maybe": ""}
MIN_EDIT_S = 0.03
SLICE_FIELDS = ["slice_id", "kind", "start_s", "end_s", "dur_s", "status", "role",
                "edited", "species_en", "species_sci"]
META_FIELDS = ["source_id", "tier", "species_en", "species_sci", "grp", "call_type",
               "recordist", "licence", "licence_url", "source_url", "quality", "ultrasonic"]

log = logging.getLogger("review")


def make_filter(expr):
    m = re.fullmatch(r"\s*(\w+)\s*(==|!=)\s*(.*?)\s*", expr)
    if not m:
        raise SystemExit(f"review: bad --filter {expr!r}, expected col==value")
    col, op, val = m.groups()
    return lambda r: (r.get(col, "") == val) == (op == "==")


class Store:
    """Manifest rows in memory, written back after every decision."""

    def __init__(self, args):
        self.dry_run = args.dry_run
        self.lock = threading.Lock()       # manifest writes
        self.prep_lock = threading.RLock()  # audio preparation and cache
        self.cfg = yaml.safe_load((ROOT / "config" / "rules.yaml").read_text())
        self.cols, self.rows = slicer.read_manifest()
        if "start_s" not in self.cols:
            raise SystemExit("review: manifest has no start_s; run "
                             "python scripts/slice.py --backfill-times first")
        self.by_id = {r["slice_id"]: r for r in self.rows}
        raws = list(csv.DictReader(slicer.RAW_MANIFEST.open(encoding="utf-8")))
        self.raws = {r["source_id"]: r for r in raws}
        keep = make_filter(args.filter)
        wanted = {r["source_id"] for r in self.rows
                  if keep(r) and r["start_s"] and (args.tier == "both" or r["tier"] == args.tier)}
        self.queue = [r["source_id"] for r in raws if r["source_id"] in wanted]
        if args.limit:
            self.queue = self.queue[:args.limit]
        self.prepared = {}  # source_id -> (y, sr, params); last two only
        self.preview = {}   # source_id -> wav bytes, dry-run only
        self.counts = Counter()
        self.files_seen = set()
        self.keys = dict(DEFAULT_KEYS)
        if KEYS.exists():
            self.keys.update(yaml.safe_load(KEYS.read_text()) or {})

    def slices(self, sid):
        out = [r for r in self.rows if r["source_id"] == sid and r["start_s"]]
        out.sort(key=lambda r: (r["kind"] != "short", float(r["start_s"])))
        return [{k: r.get(k, "") for k in SLICE_FIELDS} for r in out]

    def prepare(self, sid):
        with self.prep_lock:
            return self._prepare(sid)

    def _prepare(self, sid):
        if sid not in self.prepared:
            y, sr = slicer.load_audio(ROOT / self.raws[sid]["file"])
            y, sr_out, p, _ = slicer.prepare(y, sr, self.cfg)
            if len(self.prepared) >= 2:
                self.prepared.pop(next(iter(self.prepared)))
            self.prepared[sid] = (y, sr_out, p)
        return self.prepared[sid]

    def cache_path(self, sid):
        return CACHE / f"{slug(self.raws[sid]['species_sci'])}_xc{sid}.wav"

    def preview_wav(self, sid):
        """Prepared source audio as 16-bit WAV, peak at -1 dBFS. Cached on disk."""
        with self.prep_lock:
            return self._preview_wav(sid)

    def _preview_wav(self, sid):
        path = self.cache_path(sid)
        if path.exists():
            return path.read_bytes()
        if sid in self.preview:
            return self.preview[sid]
        y, sr, _ = self.prepare(sid)
        peak = float(np.max(np.abs(y))) or 1.0
        buf = io.BytesIO()
        sf.write(buf, y * (10 ** (-1 / 20) / peak), sr, format="WAV", subtype="PCM_16")
        data = buf.getvalue()
        if self.dry_run:
            self.preview[sid] = data
        else:
            CACHE.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return data

    def file_info(self, sid):
        info = sf.info(io.BytesIO(self.preview_wav(sid)))
        self.files_seen.add(sid)
        raw = self.raws[sid]
        meta = {k: raw.get(k, "") for k in META_FIELDS}
        meta["raw_file"] = raw["file"]
        return {"meta": meta, "duration_s": info.frames / info.samplerate, "sr": info.samplerate,
                "slices": self.slices(sid),
                "index": self.queue.index(sid) if sid in self.queue else -1,
                "total": len(self.queue)}

    def recut(self, r, start_s, end_s):
        y, sr, p = self.prepare(r["source_id"])
        a = max(0, int(round(start_s * sr)))
        b = min(len(y), int(round(end_s * sr)))
        if (b - a) / sr < MIN_EDIT_S or not np.any(y[a:b]):
            raise ValueError("edited clip is too short or silent")
        audio = slicer.shape(y[a:b].copy(), sr, p)
        snr = slicer.snr_at(slicer.frame_rms_db(y), a, b, sr, p)
        if not self.dry_run:
            sf.write(ROOT / r["slice_path"], audio, sr, subtype="PCM_24")
        r.update({"start_s": round(a / sr, 4), "end_s": round(b / sr, 4), "edited": "True",
                  "dur_s": round(len(audio) / sr, 4),
                  "peak_db": round(slicer.db(float(np.max(np.abs(audio)))), 2),
                  "rms_db": round(slicer.db(float(np.sqrt(np.mean(audio ** 2)))), 2),
                  "snr_db": round(snr, 2), "role": ""})
        r.update({k: "" for k in slicer.FEATURE_COLS})
        self.counts["edited"] += 1

    def decide(self, body):
        status = body.get("status")
        if status not in DECISIONS:
            raise ValueError(f"unknown status {status!r}")
        with self.lock:
            r = self.by_id[body["slice_id"]]
            s, e = body.get("start_s"), body.get("end_s")
            if s is not None and e is not None and (
                    abs(float(s) - float(r["start_s"])) > 1e-3
                    or abs(float(e) - float(r["end_s"])) > 1e-3):
                self.recut(r, float(s), float(e))
            r["status"], r["reject_reason"] = status, DECISIONS[status]
            self.counts[status] += 1
            if not self.dry_run:
                slicer.write_manifest(self.cols, self.rows)
            log.info("%s -> %s %s-%s", r["slice_id"], status, r["start_s"], r["end_s"])
            return {k: r.get(k, "") for k in SLICE_FIELDS}

    def save_keys(self, keys):
        unknown = set(keys) - set(DEFAULT_KEYS)
        if unknown:
            raise ValueError(f"unknown actions {sorted(unknown)}")
        merged = {**self.keys, **keys}
        if len(set(merged.values())) != len(merged):
            raise ValueError("two actions share a key")
        self.keys = merged
        if not self.dry_run:
            KEYS.write_text(yaml.safe_dump(merged, sort_keys=False))
        return merged


def handler(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            log.info("%s " + fmt, self.address_string(), *a)

        def send(self, code, body, ctype="application/json"):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def guard(self, fn):
            try:
                fn()
            except (KeyError, ValueError) as e:
                self.send(400, {"error": str(e)})
            except Exception as e:
                log.exception("request failed %s", self.path)
                self.send(500, {"error": f"{type(e).__name__}: {e}"})

        def do_GET(self):
            self.guard(self.get)

        def do_POST(self):
            self.guard(self.post)

        def get(self):
            path = unquote(urlparse(self.path).path)
            if path in ("/", "/index.html"):
                path = "/static/index.html"
            if path.startswith("/static/") and path[8:] in STATIC:
                return self.send(200, (APP_DIR / path[8:]).read_bytes(), STATIC[path[8:]])
            if path == "/api/queue":
                return self.send(200, {"queue": [
                    {"source_id": sid, "species_en": store.raws[sid]["species_en"],
                     "species_sci": store.raws[sid]["species_sci"]} for sid in store.queue],
                    "dry_run": store.dry_run})
            if path == "/api/keys":
                return self.send(200, {"keys": store.keys, "defaults": DEFAULT_KEYS})
            m = re.fullmatch(r"/api/file/(\d+)", path)
            if m:
                return self.send(200, store.file_info(m[1]))
            m = re.fullmatch(r"/audio/(\d+)\.wav", path)
            if m:
                return self.send(200, store.preview_wav(m[1]), "audio/wav")
            m = re.fullmatch(r"/slice/([\w-]+)\.wav", path)
            if m:
                return self.send(200, (ROOT / store.by_id[m[1]]["slice_path"]).read_bytes(),
                                 "audio/wav")
            self.send(404, {"error": "not found"})

        def post(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            path = urlparse(self.path).path
            if path == "/api/decide":
                return self.send(200, store.decide(body))
            if path == "/api/keys":
                return self.send(200, {"keys": store.save_keys(body)})
            if path == "/api/quit":
                self.send(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self.send(404, {"error": "not found"})

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--tier", choices=["clean", "sa", "both"], default="both")
    ap.add_argument("--filter", default="status==pending",
                    help="files with at least one matching slice are queued")
    ap.add_argument("--limit", type=int, default=0, help="max source files to queue")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    (ROOT / "data" / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=ROOT / "data" / "logs" / "review.log",
                        level=logging.INFO, format="%(asctime)s %(message)s")
    warnings.filterwarnings("ignore")
    store = Store(args)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(store))
    url = f"http://127.0.0.1:{args.port}/"
    log.info("serving %s, %d files queued", url, len(store.queue))
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    c = store.counts
    tag = " [dry-run]" if args.dry_run else ""
    print(f"review: {len(store.files_seen)} files, {c['keep']} keep, {c['reject']} reject, "
          f"{c['maybe']} maybe, {c['edited']} edited{tag}")


if __name__ == "__main__":
    main()
