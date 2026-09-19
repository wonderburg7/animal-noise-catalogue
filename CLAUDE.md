# Animal Noise Catalogue

Pipeline: fetch animal recordings -> slice into one-shots -> analyse ->
classify by rule -> human review via contact sheets -> export for Strudel
and SFZ.

## Hard rules

- Never read, open or stage an audio file into context. Audio is handled
  only by scripts in `scripts/`.
- `data/manifest.csv` is the source of truth. Query it with `head`, `rg`,
  `awk` or a one-line pandas call. Never `cat` it.
- Scripts print exactly one summary line to stdout. Verbose output goes
  to `data/logs/`. Do not read logs unless a run failed.
- Classification, role assignment and file naming are deterministic rules
  in `config/rules.yaml`. Never use a model to name or classify a sample.
- Every script is idempotent and supports `--limit N` and `--dry-run`.
  Always test with `--limit 5` first.
- Never commit `data/`. Only `library/` and code are committed.
- Every slice keeps `source`, `source_id`, `recordist`, `licence` and
  `source_url` through to export. `ATTRIBUTION.md` is generated, not
  written by hand.
- Export format is 44.1 kHz, 16-bit, mono WAV.

## Commands

    python scripts/build.py --stage fetch --limit 5
    python scripts/build.py --stage slice
    python scripts/build.py --stage analyse
    python scripts/build.py --stage contactsheet --batch 64
    python scripts/curate.py --reject "3,7,12-15"
    python scripts/build.py --stage export

## Stack

librosa, soundfile, numpy, scipy, matplotlib, requests, pyyaml,
internetarchive. FFmpeg on PATH.
