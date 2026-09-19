# Music of Nature: build spec for Claude Code

Companion to `build-plan.md`. This is the implementable version: verified
facts, exact schemas, thresholds and script contracts.

Repo: https://github.com/wonderburg7/animal-noise-catalogue

---

## 1. What I verified, and what it changes

I tested the live API with your key through the browser on your machine.
Four findings, one of which changes a decision.

### Your key works

Query `gen:Dendrocopos sp:major` returned 3,468 recordings across 35
pages. Full record schema captured in section 3.

### Python 3.14.7 is fine

Every package has a cp314 wheel: numba 0.67, librosa 1.0.0, numpy 2.5.3,
scipy 1.18.1, matplotlib 3.11.2. This was the main thing that could have
forced a downgrade. It does not.

### The licence pool is much smaller than assumed

Live counts, quality A or B unless stated:

| Filter | Recordings | Species |
|---|---|---|
| `lic:CC0` | **0** | 0 |
| `lic:BY` | **967** | 406 |
| `lic:BY` (quality A only) | 217 | 127 |
| `lic:BY-SA` (quality A only) | 4,650 | 1,476 |

Xeno-canto's effective default is non-commercial, so the large majority
of the archive is BY-NC-SA or BY-NC-ND and excluded by your policy. CC0
does not exist there at all.

**Recommendation: build two tiers from the same pipeline.**

- **Tier `clean`**: CC-BY only. 967 recordings, 406 species. Attribution
  is the only obligation. Safe for anything, including publishing and
  teaching material.
- **Tier `sa`**: CC-BY-SA. Roughly five times the material. Tagged
  separately, exported to a separate Strudel bank, so share-alike can
  never contaminate a track by accident.

Cost of the second tier is close to zero, since it is the same code with
a different filter. You choose at the point of making music, not now.

The alternative is clean-only, which is simpler but gives up most of the
archive for an obligation you may never trigger.

### `lic:` filters server-side, with exact matching

`lic:BY` returns 967, not everything containing "by". So we filter in the
query and never download a file we cannot use. This is the single biggest
bandwidth and time saving in the pipeline.

### Two bonus findings worth designing around

- **Xeno-canto is not just birds.** The `grp` field carries `birds`,
  `grasshoppers`, `bats`, `frogs`. Verified: `lic:BY q:">C"
  grp:grasshoppers` returns 57 recordings across 29 species. Crickets and
  bush-crickets are excellent texture and rhythm material, and bat clicks
  are natural percussion. Do not restrict the catalogue to birds.
- **The `also` field is a free purity signal.** It lists other species
  audible in the recording. `also: []` means a clean single-species
  recording, which is exactly what slices well. Sort by it.

---

## 2. Two housekeeping notes

**Your API key is now in this conversation's history.** It is a free
read-only key, so the stakes are low, but it must never be committed.
Put it in `.env`, confirm `.env` is in `.gitignore` before the first
commit, and regenerate it in your xeno-canto account settings if you want
to be tidy. I have not written it to the plan, the spec or any saved note.

**Xeno-canto blocks datacentre traffic.** Requests from Anthropic's cloud
were refused by their bot protection; the same request from your machine
worked. Expected consequence: the pipeline runs on your machine, as
planned. Give the fetch script a descriptive User-Agent and a one-second
delay between requests so your university IP stays welcome.

---

## 3. Xeno-canto v3 record schema, verified

```
id gen sp ssp grp status en rec cnt loc lat lon alt type sex stage
method url file file-name sono{small,med,large,full} osci{small,med,large}
lic q length time date uploaded also rmk animal-seen playback-used
temp regnr auto dvc mic smp annotation-set
```

Response envelope: `numRecordings` (string), `numSpecies` (string),
`page` (int), `numPages` (int), `recordings` (array).

Parsing notes, all of which bite if missed:

- `lic` is a **URL**, for example
  `https://creativecommons.org/licenses/by-nc-nd/4.0/`. Extract the path
  segment to get `by-nc-nd`. Handle a missing scheme (`//creativecommons...`).
- `length` is `"M:SS"` or `"H:MM:SS"`, a string. Parse to seconds.
- `numRecordings` and `smp` are strings. Cast.
- `file` is `https://xeno-canto.org/{id}/download` and redirects. Follow
  it and sniff the content type; originals may be wav, mp3 or flac.
- `file-name` contains unicode and commas. Never use it as a local path.
  Use `XC{id}` plus the sniffed extension.
- `per_page` is unverified on v3. Page with `&page=N` against `numPages`.

Reject on ingest: `smp < 22050`, `q` in `{D, E}`, licence containing `nc`
or `nd`.

---

## 4. `config/targets.yaml`

Query by **sonic role**, not by species list. The licence filter thins the
pool unpredictably per species, so let the pool decide which species you
get. This is more robust than any list I could write, and it costs nothing
to widen later.

```yaml
xeno_canto:
  base_query: 'q:">C"'
  tiers:
    clean: 'lic:BY'
    sa:    'lic:BY-SA'
  per_query_max: 6          # recordings, not slices
  prefer_no_also: true      # rank recordings with empty `also` first
  max_len_s: 120
  queries:
    - { id: woodpecker_drum, role_hint: perc,    q: 'type:drumming' }
    - { id: bat_clicks,      role_hint: perc,    q: 'grp:bats' }
    - { id: frog_calls,      role_hint: perc,    q: 'grp:frogs' }
    - { id: cricket_buzz,    role_hint: texture, q: 'grp:grasshoppers' }
    - { id: owl_hoots,       role_hint: drone,   q: 'gen:Strix' }
    - { id: cuckoo,          role_hint: tonal,   q: 'gen:Cuculus type:song' }
    - { id: curlew,          role_hint: tonal,   q: 'gen:Numenius' }
    - { id: nightingale,     role_hint: tonal,   q: 'gen:Luscinia type:song' }
    - { id: corvid_rasp,     role_hint: texture, q: 'gen:Corvus type:call' }
    - { id: wader_pipes,     role_hint: tonal,   q: 'gen:Tringa' }
    - { id: grebe_wail,      role_hint: drone,   q: 'gen:Podiceps' }
    - { id: tit_chatter,     role_hint: perc,    q: 'gen:Parus' }
```

**`fetch_xc.py --probe` is mandatory before any download.** It runs every
query with both tier filters, reads only `numRecordings` and `numSpecies`,
and prints one table. Queries returning zero get reported, not silently
skipped. This is how the config gets validated without spending bandwidth
or tokens, and it is the first thing to run.

---

## 5. `data/manifest.csv` schema

One row per slice. The single source of truth. Never regenerated from
scratch once curated; always merged on `slice_id`.

```
slice_id          xc1175420_03            stable, source id + slice index
tier              clean | sa
source            xc | freesound | watkins
source_id         1175420
source_url        https://xeno-canto.org/1175420
recordist         Ulf Elman
licence           by | by-sa
licence_url       https://creativecommons.org/licenses/by/4.0/
species_sci       Dendrocopos major
species_en        Great Spotted Woodpecker
grp               birds | grasshoppers | bats | frogs
call_type         call, drumming
role_hint         perc                    from targets.yaml
role              perc | tonal | texture | drone   assigned by rule
file              library/perc/perc_dendrocopos-major_xc1175420_03.wav
dur_s             0.184
peak_db           -1.0
rms_db            -14.2
centroid_hz       3820
f0_hz             null
f0_conf           0.12
midi_note         null
cents_off         null
tonal_ratio       0.21
onset_sharpness   0.83
snr_db            21.4
status            keep | reject | pending
reject_reason     ""
```

---

## 6. `config/rules.yaml`

Starting thresholds. Expect to tune `delta` and `min_snr_db` after the
first contact sheet.

```yaml
slice:
  target_sr: 48000
  highpass_hz: 80
  onset:
    hop_length: 512
    backtrack: true
    delta: 0.07
    wait: 10
  min_dur_s: 0.03
  max_dur_s: 8.0
  pad_s: 0.01
  fade_ms: 5
  peak_dbfs: -1.0
  min_snr_db: 8.0
  max_clip_ratio: 0.001
  max_slices_per_file: 12      # stop one long dawn chorus dominating

classify:
  perc:    { max_dur_s: 0.35, min_onset_sharpness: 0.60 }
  tonal:   { min_tonal_ratio: 0.60, min_dur_s: 0.10, max_dur_s: 2.0, min_f0_conf: 0.50 }
  drone:   { min_dur_s: 2.00, min_tonal_ratio: 0.50 }
  # anything unmatched falls through to texture

export:
  sr: 44100
  bit_depth: 16
  channels: 1
```

Feature definitions, so they are computed the same way every time:

- `tonal_ratio` = harmonic RMS / (harmonic + percussive RMS), from
  `librosa.effects.hpss`.
- `onset_sharpness` = 1 - (time from slice start to peak amplitude /
  slice duration). Near 1 means an instant attack.
- `snr_db` = 90th percentile frame RMS minus 10th percentile frame RMS,
  in dB, over the source recording around the slice.
- `f0_hz`, `f0_conf` from `librosa.pyin`, `fmin=80`, `fmax=12000`,
  median of voiced frames.

---

## 7. Script contracts

Every script: `--limit N`, `--dry-run`, `--tier clean|sa|both`, one
summary line to stdout, verbose output to `data/logs/<script>.log`.

```
fetch_xc.py --probe
    Count-only pass over all queries and tiers. No downloads.
    Out: table + data/probe_report.csv

fetch_xc.py --tier clean [--limit N]
    Downloads originals. Reads/writes data/state/xc.json for idempotency.
    Ranks candidates: `also` empty first, then quality, then smp desc.
    Out: data/raw/xc/XC{id}.{ext}, rows in data/raw_manifest.csv
    Summary: "fetch_xc: 38 new, 12 cached, 4 skipped (licence), 1 failed"

fetch_freesound.py    Same contract. Blocked on your API approval.
                      Try token auth for downloads; fall back to
                      high-quality preview MP3s if OAuth2 is required.

fetch_watkins.py      Bulk-fetch via `internetarchive` from item
                      `watkins-marine-mammal-sound-database-full-cuts`.
                      Tier: `restricted`. Never exported to the public
                      repo (see section 9).

slice.py              raw -> data/slices/*.wav + manifest rows
analyse.py            fills the feature columns in manifest.csv
classify.py           fills `role` from rules.yaml. Pure function of
                      the feature columns, so it is re-runnable after
                      any threshold change without re-slicing.

contactsheet.py --batch 64 --filter "status==pending"
    Out: review/sheet_NNN.png   8x8 mel spectrograms, index-labelled
         review/sheet_NNN.wav   same 64 in order, 40ms click between
    Summary: "contactsheet: 3 sheets, 187 pending slices"

curate.py --sheet 7 --reject "3,7,12-15,40"
    Maps sheet-local indices to slice_ids, sets status, writes
    reject_reason="manual". Idempotent and reversible.

export_strudel.py --tier clean
export_sfz.py --tier clean
build.py --stage fetch|slice|analyse|classify|contactsheet|export
```

---

## 8. Naming and export

```
library/<tier>/<role>/<role>_<species-slug>_<sourceid>_<nn>[_<note>].wav
library/clean/perc/perc_dendrocopos-major_xc1175420_03.wav
library/clean/tonal/tonal_cuculus-canorus_xc198442_01_d5.wav
```

Lowercase throughout, hyphens inside a field, underscores between fields,
ASCII only. Export as 44.1 kHz, 16-bit, mono WAV: one format that both
Strudel and essentially every hardware sampler reads without complaint.

`strudel.json` banks, named so the tier is visible while you type:

```json
{
  "_base": "https://raw.githubusercontent.com/wonderburg7/animal-noise-catalogue/main/library/clean/",
  "perc":        ["perc/perc_dendrocopos-major_xc1175420_03.wav", "..."],
  "woodpecker":  ["perc/perc_dendrocopos-major_xc1175420_03.wav", "..."],
  "cuckoo":      ["tonal/tonal_cuculus-canorus_xc198442_01_d5.wav"]
}
```

Two banks per sample: one by role (`perc`, `tonal`, `texture`, `drone`)
for round-robin variation, one by common-name slug for when you want that
specific animal. Share-alike material goes in a second file,
`strudel-sa.json`, loaded separately and deliberately.

SFZ: pitched slices mapped by `pitch_keycenter` to `midi_note`; percussive
slices laid across a drum map with `seq_length` round-robin groups.

---

## 9. Licence and attribution rules

- `ATTRIBUTION.md` is generated from the manifest, grouped by recordist,
  listing source ID, species and licence URL. CC-BY requires this and it
  costs nothing to automate.
- `clean` and `sa` never share a directory, a JSON file or an SFZ file.
- **Watkins material is not Creative Commons.** WHOI makes it freely
  available for research and education, which is not permission to
  publish derivatives. It stays in `library/restricted/`, which is
  gitignored and never pushed. Use it locally, do not publish it, and
  check their terms before that changes.

---

## 10. Acceptance tests

So Claude Code can verify without you listening to anything.

1. `fetch_xc.py --probe` reports non-zero counts for at least 10 of the
   12 queries in tier `clean`.
2. No file in `data/raw/` has a licence containing `nc` or `nd`.
3. Re-running any fetch downloads zero new files.
4. Every slice: duration within `[min_dur_s, max_dur_s]`, peak within
   0.5 dB of -1 dBFS, no sample at full scale.
5. Every manifest row has a non-empty `recordist`, `licence` and
   `source_url`.
6. Every path in `strudel.json` exists on disk and is under 2 MB.
7. `classify.py` re-run produces an identical `role` column.
8. Round-trip: a random exported WAV decodes at exactly 44100 Hz, 16-bit,
   1 channel.

---

## 11. Next physical step

Open the repo folder in Claude Code and give it this spec plus
`build-plan.md`. Tell it to build in this order and stop after each:

1. Scaffold the repo, `.gitignore` with `.env` and `data/` first.
2. `fetch_xc.py --probe` only. Show me the table before anything downloads.

The probe is the right first milestone: it validates the key, the query
syntax, the licence filter and the config, and it downloads nothing.

Once Freesound approves your application, tell me and I will add its
query config and the mammal and insect coverage that xeno-canto lacks.
