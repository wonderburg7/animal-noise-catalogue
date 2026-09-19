# Spec revision 1: after the probe

Applies to `build-spec.md` sections 4 and 6. Everything else stands.

The probe did its job. It found one real bug in my spec and one wrong
strategic assumption, both before a single byte was downloaded.

---

## 1. What I checked, and the verdicts

| Anomaly | Verdict |
|---|---|
| `grebe_wail` 0 in both tiers | **Real.** `gen:Podiceps` has 1,079 recordings across 8 species, and not one is BY or BY-SA. Every grebe on xeno-canto is NC or ND. |
| `cricket_buzz` sa 23,622 | **Real.** Confirmed directly. There is a very large BY-SA orthoptera collection. |
| `cuckoo` 0 in clean | **Real**, and a symptom of the strategy problem below. |

Nothing is broken in the script. The counts are the truth.

---

## 2. The wrong assumption: genus-targeting

Group the clean tier by musical role:

| Role | Clean recordings | Of which |
|---|---|---|
| perc | 291 | 280 are frogs |
| texture | 60 | 57 are crickets |
| **tonal** | **22** | across 8 species |
| **drone** | **6** | one genus |

Tonal and drone are precisely the roles that carry melody and harmony,
and they are nearly empty. But the shortage is self-inflicted:

| Query | Clean recordings / species |
|---|---|
| The four genus-based tonal queries combined | 22 / 8 |
| **`type:song`, no genus restriction** | **242 / 147** |
| **`type:call`, no genus restriction** | **138 / 92** |

Eleven times the material and eighteen times the species diversity, from
deleting a constraint.

**My error.** Last round I dropped the hardcoded species list because the
licence filter thins each species unpredictably. That reasoning applies
just as forcefully to genus, and I only carried it half way. When a filter
has already removed 95% of the archive, adding `gen:Cuculus` on top is
asking for the intersection of two narrow sets. Query by **call type**
across the whole archive and let the licence filter choose the species.

You lose the ability to say "I want a cuckoo". You gain 147 species of
song instead of 8. For building a sample library, breadth wins, and you
can always add a named-species query later for a specific track.

---

## 3. The real bug: bats are ultrasonic

Verified on a live record: `grp:bats` recordings carry **`smp: "500000"`**.
Half a megahertz.

Bat echolocation sits between 20 and 120 kHz. The pipeline as specified
resamples everything to 48 kHz, whose Nyquist limit is 24 kHz. Applied to
a bat file it would discard the entire signal and produce silence or
aliasing artefacts. The probe cost nothing and caught this; the first
download would have produced 30 files of nothing.

**Fix: reinterpret the sample rate, do not resample.** Read the samples
and write them out declaring 44.1 kHz. This is the standard bat-detector
time-expansion trick, not a workaround.

A 500 kHz recording replayed at 44.1 kHz is slowed 11.3x and drops about
42 semitones. Echolocation clicks at 40 kHz land around 3.5 kHz. Fourteen
seconds of recording becomes two and a half minutes of chirps and sweeps.
This is probably the most interesting raw material in the whole catalogue.

Also noted: `type:echolocation` is a valid type value, which makes the bat
query precise.

---

## 4. Revised `config/targets.yaml`

```yaml
xeno_canto:
  base_query: 'q:">C"'
  tiers:
    clean: 'lic:BY'
    sa:    'lic:BY-SA'
  max_len_s: 120
  prefer_no_also: true
  defaults:
    max_recordings: 40
    max_per_species: 2

  queries:
    # tonal: the melodic core, broad on purpose
    - { id: song_broad,   role_hint: tonal,   q: 'type:song',
        max_recordings: 120, max_per_species: 1 }

    # perc: short articulations
    - { id: call_broad,   role_hint: perc,    q: 'type:call',
        max_recordings: 80,  max_per_species: 1 }
    - { id: frog_calls,   role_hint: perc,    q: 'grp:frogs',
        max_recordings: 60,  max_per_species: 1 }
    - { id: drumming,     role_hint: perc,    q: 'type:drumming',
        max_recordings: 25,  max_per_species: 3 }

    # texture
    - { id: cricket_buzz, role_hint: texture, q: 'grp:grasshoppers',
        max_recordings: 60,  max_per_species: 1 }
    - { id: alarm_rasp,   role_hint: texture, q: 'type:"alarm call"',
        max_recordings: 40,  max_per_species: 1 }

    # ultrasonic: separate processing path, see rules.yaml
    - { id: bat_echo,     role_hint: perc,    q: 'grp:bats type:echolocation',
        max_recordings: 30,  max_per_species: 2, ultrasonic: true }

  # No drone query. classify.py already derives drone from any slice
  # over 2s with tonal_ratio > 0.5, so it emerges from the song pool.
  # The owl and grebe queries were solving a solved problem.
```

Two changes to how caps work, both mattering:

- **`per_query_max: 6` is replaced by `max_recordings` plus
  `max_per_species`.** The old single cap meant `cricket_buzz` would
  contribute 6 recordings out of 23,622 available, which wastes the best
  texture source on the site.
- **`max_per_species: 1` on the broad queries** is what converts a large
  pool into diversity rather than 120 recordings of the same blackbird.

Note the quoting: `type:"alarm call"` needs the inner quotes because the
value contains a space.

---

## 5. Additions to `config/rules.yaml`

```yaml
ultrasonic:
  detect_smp_above: 96000
  method: reinterpret          # relabel the header, never resample
  target_playback_sr: 44100
  truncate_source_s: 3.0       # truncate BEFORE slowing, not after
  highpass_hz: 0               # the 80 Hz filter is meaningless here
  max_slices_per_file: 40      # a slowed bat pass has many clicks
  min_snr_db: 4.0              # detector recordings are clean but quiet
```

`truncate_source_s` is the one to get right. Three seconds of source at
500 kHz becomes 34 seconds of output. Without it, a 14-second file
becomes 2.6 minutes and the onset detector has to chew through all of it.

---

## 6. What to expect from the first real fetch

Clean tier, with the revised config: roughly 350 to 400 recordings, which
at 6 to 10 slices each is 2,500 to 3,500 slices. That is a lot of contact
sheets.

**Run the first fetch with `--limit 40`.** That gives about 300 slices,
five contact sheets, and enough signal to tune the slicing thresholds
before committing to the full run. Tuning `delta` and `min_snr_db` after
seeing 3,000 slices is a waste of your attention.

---

## 7. Prompt for Claude Code

```
Read docs/spec-revision-1.md. It revises build-spec.md sections 4 and 6
based on the probe results.

1. Replace config/targets.yaml and add the ultrasonic block to
   config/rules.yaml, both verbatim from that document.

2. Re-run fetch_xc.py --probe against the new config and show me the table.
   I want to confirm the new queries return what the revision predicts
   before anything downloads.

3. Then stop.

Do not download audio yet. Do not write slice.py yet.
```

The second probe is nearly free and confirms the new query syntax, in
particular the quoting on `type:"alarm call"`, which is the one line most
likely to fail silently and return zero.
