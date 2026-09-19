# Music of Nature: build plan

Animal sounds sourced, isolated and catalogued automatically, ready to play
in Strudel and in hardware samplers. Written to be handed to Claude Code.

Status: plan only. Nothing built yet. Folder on Desktop is empty.

---

## 1. The core idea, in one line

Claude writes the code once; Python touches every audio file after that.
The model never sees a waveform, a directory listing or a per-file log,
only a CSV manifest and, at review time, one picture.

### The seven rules that keep this cheap

1. **Audio bytes never enter the context.** No staging WAVs, no reading
   binaries, no describing sounds to the model.
2. **One manifest is the source of truth.** `data/manifest.csv`, one row
   per slice. Claude reads it with `head`, `rg` and `awk`, never `cat`.
3. **Scripts print a summary, not a log.** Each run ends with one line:
   `fetch_xc: 41 new, 6 skipped (licence), 3 failed`. Verbose output goes
   to a log file the model does not read unless something breaks.
4. **Review is one PNG plus one WAV per 64 slices.** You look and listen
   once, reply with a list of integers to bin. That is the only human step.
5. **Classification is rule-based, not model-based.** Thresholds on
   measured features. No LLM in the per-sample loop, ever.
6. **Everything is idempotent.** State files per source mean a re-run
   costs nothing and downloads nothing twice.
7. **`--limit` and `--dry-run` on every script.** Debug on five files,
   not five hundred.

Expect the whole build to cost a few Claude Code sessions, then near-zero
per batch afterwards.

---

## 2. Decisions taken

| Decision | Choice |
|---|---|
| Where it runs | Claude Code, locally on Windows |
| Targets | Strudel and MIDI/sampler, both generated from one catalogue |
| Licences | CC0, CC-BY, CC-BY-SA only |
| Curation | Auto-slice, then spectrogram contact sheets for review |
| Hosting | Public GitHub repo, loaded via Strudel's `github:` shorthand |
| First batch | ~20 to 30 species chosen for sonic role, not taxonomy |
| Sources | xeno-canto, Freesound, Watkins Marine Mammal Sound Database |

---

## 3. Repository layout

```
animal-noise-catalogue/
  CLAUDE.md               rules of engagement for Claude Code (section 8)
  README.md
  .env                    API keys, gitignored
  .gitignore
  requirements.txt
  config/
    targets.yaml          what to fetch, per source, with caps
    rules.yaml            slicing and classification thresholds
  scripts/
    fetch_xc.py
    fetch_freesound.py
    fetch_watkins.py
    slice.py
    analyse.py
    classify.py
    contactsheet.py
    curate.py
    export_strudel.py
    export_sfz.py
    build.py              runs the pipeline end to end
  data/
    raw/                  downloaded originals, gitignored
    state/                per-source JSON, gives idempotency
    manifest.csv          one row per slice, the source of truth
    reject.txt            slice ids you binned at review
    logs/                 verbose output, gitignored
  review/                 contact sheets and montages, gitignored
  library/                the publishable output, committed
    perc/ tonal/ texture/ drone/
    strudel.json
    instruments/*.sfz
    ATTRIBUTION.md
```

Only `library/` and the code are committed. Raw downloads stay local.

---

## 4. Pipeline stages

### Stage 0: config

`config/targets.yaml` drives everything, so adding species later is a
config edit rather than a code change.

```yaml
xeno_canto:
  licences: [cc0, by, by-sa]
  quality: [A, B]
  max_len_s: 60
  per_query: 4
  queries:
    - { q: "gen:Dendrocopos sp:major type:drumming", role_hint: perc }
    - { q: "gen:Cuculus sp:canorus type:song",       role_hint: tonal }
    - { q: "gen:Strix sp:aluco",                     role_hint: drone }
freesound:
  licences: ["Creative Commons 0", "Attribution", "Attribution NonCommercial"]
  queries:
    - { q: "cicada", role_hint: texture }
    - { q: "frog croak", role_hint: perc }
watkins:
  archive_item: watkins-marine-mammal-sound-database-full-cuts
  species: [humpback whale, killer whale, beluga]
```

### Stage 1: fetch

Three small scripts, one per source, all writing the same `raw/` layout
and appending to `data/state/<source>.json`.

- **xeno-canto**: API v3, needs a key (section 6). Filter on licence,
  quality rating and length before downloading anything. Record recordist,
  licence, licence URL and recording URL for attribution.
- **Freesound**: search via API token. If downloading originals turns out
  to need OAuth2, fall back to the high-quality preview MP3s, which are
  more than good enough once chopped to one-shots. Decide this at build
  time by trying it, not by guessing.
- **Watkins**: skip scraping. The full database is mirrored on the
  Internet Archive as a bulk item, so `internetarchive`'s `ia download`
  with a glob fetches it in one go.

Output: `data/raw/<source>/<id>.<ext>` plus a row per recording in
`data/raw_manifest.csv`.

### Stage 2: slice

Per raw file, with `librosa` and `soundfile`:

1. Decode to mono, resample to 48 kHz, float32.
2. High-pass at 80 Hz to strip handling rumble and wind.
3. Segment on onsets (`librosa.onset.onset_detect`) with an RMS gate, so
   a bird singing eight phrases yields eight slices, not one.
4. Trim silence, pad 10 ms, apply 5 ms fades in and out.
5. Peak-normalise to -1 dBFS.
6. Reject slices that fail the gates in `rules.yaml`: shorter than 30 ms,
   longer than 8 s, estimated SNR below ~8 dB, clipped, or dominated by
   broadband noise.

Output: `data/slices/*.wav` plus rows appended to `data/manifest.csv`.

### Stage 3: analyse

Per slice, measured not guessed:

`dur_s`, `peak_db`, `rms_db`, `spectral_centroid_hz`, `f0_hz` (pyin),
`midi_note`, `cents_off`, `tonal_ratio` (harmonic RMS over total, from
HPSS), `onset_sharpness` (attack time to peak), `snr_db`.

### Stage 4: classify

Pure thresholds, tunable in `rules.yaml`:

- `perc` if `dur_s < 0.35` and attack is sharp
- `tonal` if `tonal_ratio > 0.6`, f0 confident, `0.1 < dur_s < 2.0`
- `drone` if `dur_s > 2.0` and `tonal_ratio > 0.5`
- `texture` otherwise

### Stage 5: contact sheet review

This is the only step that costs your attention.

`contactsheet.py` emits, per batch of 64 slices:

- `review/sheet_007.png`: an 8 x 8 grid of mel spectrograms, each labelled
  with its index, duration and role.
- `review/sheet_007.wav`: the same 64 slices in the same order,
  concatenated with a short click between them.

You look at the picture while playing the WAV, then reply with something
like `bin 3, 7, 12-15, 40`. `curate.py` reads that, writes `reject.txt`
and moves the losers out. One image plus one integer list per 64 samples
is as cheap as review gets.

### Stage 6: export

Naming convention, deterministic, so nothing needs a model to name it:

```
library/<role>/<role>_<species-slug>_<sourceid>_<nn>[_<note>].wav
library/perc/perc_dendrocopos-major_XC421337_03.wav
library/tonal/tonal_cuculus-canorus_XC198442_01_D5.wav
```

Export format: **44.1 kHz, 16-bit, mono WAV**. One format that both
Strudel and essentially every hardware sampler will read without fuss.

- `export_strudel.py` writes `library/strudel.json` with `_base` set to
  the repo's raw URL and banks grouped by role and by species, so you can
  write `s("cuckoo")` or `s("perc")` and get round-robin variation.
- `export_sfz.py` writes SFZ instruments: pitched slices mapped by
  `pitch_keycenter` to their detected note, percussive slices laid out
  across a drum map with round-robin groups.
- `ATTRIBUTION.md` is generated from the manifest, grouped by recordist,
  with source ID and licence. CC-BY requires this.

### Stage 7: publish

`git add library/ && git commit && git push`, then in Strudel:

```javascript
await samples('github:yourname/animal-noise-catalogue')
s("perc*4").gain(0.8)
```

---

## 5. Risks and things I have not verified

- **xeno-canto API key.** A third-party client's docs state a key became
  mandatory on 10 October 2025 for API v3. Xeno-canto's own API page was
  behind bot protection when I checked, so confirm the exact process when
  you register. My direct API calls without a key returned empty.
- **Freesound downloads.** Token auth definitely covers search. The docs
  do not clearly say whether downloading originals needs OAuth2. Plan for
  the preview-MP3 fallback.
- **CC-BY-SA is viral.** Share-alike can propagate to anything you publish
  that contains those samples. The manifest keeps a `licence` column, and
  the exporter should take `--licence cc0,by` so you can build a clean
  subset for anything public. Recommend doing your first batch CC0 and
  CC-BY only, and treating BY-SA as a later, deliberate choice.
- **Watkins is not Creative Commons.** WHOI makes it freely available for
  research and education, which is not the same as a licence to publish
  derivatives. Treat marine mammal material as personal and teaching use
  until you have checked their terms.
- **Field recordings are messy.** Wind, traffic and other species will get
  through the SNR gate sometimes. That is what the contact sheets catch.
- **Assumption worth challenging:** that you want raw naturalistic slices.
  Some of the best results will come from slices that are barely
  recognisable as animals. Do not over-filter for "clean call".

**One alternative, briefly:** if this turns out to be more engineering
than fun, commercial wildlife sample packs exist, pre-cleaned and
licensed for music. Cheaper in time, far less interesting, and you lose
the ability to go and find the exact species you want.

---

## 6. Set-up, your actions

In order. Each is a few minutes.

1. **xeno-canto key.** Register at xeno-canto.org, verify the email, copy
   the API key from account settings.
2. **Freesound key.** Register at freesound.org, apply for an API key at
   `freesound.org/apiv2/apply`.
3. **GitHub repo.** Create an empty **public** repo named
   `animal-noise-catalogue`. Public is required: Strudel loads over HTTP
   and a private repo will not serve.
4. **FFmpeg.** In PowerShell: `winget install Gyan.FFmpeg`
5. **Python.** 3.11 or later. Confirm with `python --version`.
6. **Open the folder in Claude Code** and give it this plan.

Keys go in `.env`, which is gitignored:

```
XC_API_KEY=...
FREESOUND_API_KEY=...
```

---

## 7. Instructions for this Claude Project

Paste into the Project's custom instructions:

> This project designs and refines a pipeline that sources animal
> recordings from xeno-canto, Freesound and the Watkins database, slices
> them into one-shots, catalogues them by measured acoustic features and
> exports them for Strudel and for hardware samplers.
>
> This Project plans; Claude Code implements. Produce plan and spec
> documents here, not implementations.
>
> Token discipline is a hard requirement. Never read, stage or describe
> audio files. Work only from `data/manifest.csv` and summary output.
> Never put an LLM in a per-sample loop: classification and naming are
> deterministic rules. Review happens through contact-sheet images, with
> my reply being a list of indices.
>
> Licence metadata is not optional. Every sample carries its source,
> recordist and licence through to `ATTRIBUTION.md`.

---

## 8. CLAUDE.md for the repo

Paste into `CLAUDE.md` at the repo root, so Claude Code does not have to
re-derive any of this:

```markdown
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
```

---

## 9. Next physical step

Register for the xeno-canto API key. Everything else waits on that one
thing, and it takes about two minutes.

Then tell me, and I will write the detailed build spec for Claude Code:
exact function signatures, the manifest schema, the `rules.yaml`
thresholds and the species list for the first batch.
