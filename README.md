# plaudvault

Own your Plaud recordings. Their cloud becomes a sync pipe; transcription,
summarization, and storage happen on your machine — and the recordings turn into
tracked actions you can actually measure.

```
Plaud device ──BLE──> Plaud app ──> Plaud cloud
                                        │
                                        │  download + verify
                                        ▼
                              your archive directory
                                audio/YYYY/MM/*.mp3
                                transcripts/*.txt     ← local Whisper
                                summaries/*.md        ← local LLM
                                sentiment             ← local LLM, in the manifest
                                stack/*.txt           ← only what you approved
                                manifest.sqlite
                                        │
                                        ▼
                    console: triage · actions · trends · measures
                                        │
                     ┌──────────────────┼──────────────────┐
                     ▼                  ▼                  ▼
              notes in your      actions with          delete from
              vault (optional)   outcomes              Plaud's cloud
```

## Why

Plaud charges for **transcription minutes**, not storage — storage is free and
unlimited. That's the seam. Let their cloud hold the audio, pull the original MP3
down over their API, and never let their AI touch it. Your free monthly minutes stay
unspent, and the transcript never leaves your machine.

**What this does not do:** get audio off the device without their cloud. Plaud
disabled raw USB file access in firmware 2.1, and the pin models never had it. Audio
travels device → app → cloud, full stop. "Private" here means *your storage, your
transcription, your summaries, and delete-after-archive* — not
never-touches-their-servers. If that distinction matters to you, it should, and no
amount of software on this end changes it.

## Documentation

- **[Product bible](docs/PRODUCT-BIBLE.md)** — what this is, every contested technical
  decision with the reasoning that settled it, the backlog, the roadmap, and the known
  limits. Its status sections regenerate from git and the live archive.
- **Diagrams** — [architecture](docs/diagrams/architecture.png) ·
  [data model](docs/diagrams/data-model.png) ·
  [user journeys](docs/diagrams/user-journeys.png) (editable `.excalidraw` sources alongside)

## Requirements

- Python 3.11+
- A Plaud account with recordings in it
- **Transcription**: an Apple Silicon Mac gets GPU transcription automatically via
  `mlx-whisper`. Everywhere else uses `faster-whisper` (CPU, or CUDA if present).
  Both install automatically for your platform.
- **Summaries and action extraction**: [Ollama](https://ollama.com) for a fully local
  setup, or any OpenAI-compatible endpoint (LM Studio, llama.cpp, vLLM, OpenRouter,
  Groq, OpenAI).

## Install

```bash
git clone https://github.com/Adawodu/plaudvault
cd plaudvault
pip install -e .          # or: uv sync

plaudctl init             # interactive setup
plaudctl login you@example.com
plaudctl run
plaudctl web
```

`init` asks where to store the archive, whether you want markdown notes, which
transcription backend, and which model. Re-run it any time to change your mind.

Configuration lives at `~/.config/plaudvault/config.toml` (`%APPDATA%` on Windows).
Every key can be overridden with `PLAUDVAULT_<KEY>` in the environment.

## Daily use

```bash
plaudctl run       # sync → transcribe → summarize → score tone → notes → extract
plaudctl fresh     # is the vault up to date? (--cloud also asks Plaud)
plaudctl status    # what's archived, what's pending, what's healthy
plaudctl verify    # re-hash the archive, catch bitrot or missing files
plaudctl web       # the console
```

### Running it automatically

```bash
plaudctl service install                 # console always on, sync 4x/day
plaudctl service install --hours 8,13,18 # or pick your own
plaudctl service status
plaudctl service uninstall
```

macOS gets launchd agents; Linux gets systemd user units and a timer. The console is
kept alive and restarted if it dies; sync runs on the schedule. **Neither ever
prunes** — deleting from Plaud's cloud is always something you do deliberately.

On Linux, `sudo loginctl enable-linger $USER` keeps the console running after logout.

## The console

`plaudctl web` → http://127.0.0.1:8787. A **Help** page inside the app explains the
workflow.

- **Inbox** — transcribed but untriaged recordings.
- **Library** — everything, filterable by tier.
- **Recording** — player, summary, transcript with click-to-seek timestamps, its tone
  reading, and its actions. Where you set the tier and mark for cloud deletion.
- **Actions** — accept a proposal (stating what it should achieve), work it, complete
  it with an outcome score.
- **Trends** — tone over time, one dot per recording. Click a dot to open it.
- **Systems** — recurring commitments promoted to named practices with adherence rates.
- **Measures** — follow-through, outcomes, systems, capture-pipeline health.
- **Vault status** — behind the freshness pill in the header: what still needs
  processing, and whether Plaud is holding anything this disk has never seen.

Binds to 127.0.0.1 with no authentication, because it serves your recordings off a
local disk. Don't expose it. If you need remote access, put a real identity proxy in
front of it.

## Triage, and why tiering is physical

Every recording gets one of three tiers:

- **stack** — the transcript is *copied* into `stack/`, the only directory meant to be
  indexed by a search or knowledge tool.
- **local** — archived and searchable in the console, kept out of that corpus.
- **exclude** — noise.

Re-tier something out of *stack* and the copy is deleted on the next sync. The
decision is enforced by what exists on disk, not by a flag downstream tools have to
remember to respect.

This matters more than it sounds. A wearable recorder captures whoever is in earshot —
family, colleagues, strangers — and none of them opted in. Defaulting personal
recordings to *local* is the difference between an archive and a surveillance corpus.

## Actions

`plaudctl extract` reads each transcript and proposes **commitments** — things a person
actually said they would do. Everything arrives as `proposed` and does nothing until you
accept it. Plenty of recordings contain nothing actionable, and the extractor returns an
empty list for those rather than manufacturing work.

`--suggestions` (or `extract_suggestions = true`) also asks for implied next steps.
It is off by default because a small local model is bad at the judgment it requires:
on a real ~20-hour corpus (33 recordings) it returned **198 suggestions against 57
commitments**, and the suggestions were largely topic summaries — *"Discuss the app's features"*, *"Share the
screen to show the app concept"* (which had already happened), *"Secure and compliant
infrastructure for managing IP"* (not an action at all). A 255-item board is one you
stop opening, and a board nobody opens measures nothing.

Suggestions are removed from the prompt entirely rather than filtered from the response.
A category that is merely *mentioned* is one the model will populate, so when they are
off the word does not appear in the rules, the schema, or the worked example.

### Every quote is checked against the transcript

Each proposal carries the line it came from, and that line is verified against the text
the model was actually given. This is not paranoia: on the corpus above, two proposals
were verbatim copies of the prompt's own worked example — an action to *"Schedule a
review call with Dana"* quoting *"I need to email Dana to set up the review call"*, when
the word "Dana" appears in none of the recordings. A small model will sometimes return
the example instead of reading the input.

That failure mode is worse than a wrong action. The quote exists so you can check the
action against the recording, so a fabricated quote defeats the audit it is there to
support — it reads as evidence and is not.

Verbatim matching alone is too strict, because models legitimately elide and reword; on
the real corpus it would have discarded 23 sound actions to catch 2 bad ones. A quote
passes if a 40-character run appears verbatim, or if at least 60% of its content words
do. That keeps 253 of 255 and drops exactly the two leaks. Drops are printed, never
silent.

Accepting asks for an **intent**: what this is supposed to achieve. Outcome scoring
later is judged against exactly that, because finishing a task and the task having
worked are different things and only one is worth measuring.

## Semantic search

Keyword search fails on speech. You remember someone talking about being underpaid; the
recording says *"they went below the range that I gave."* No substring links those, and
the recording stays lost. Embeddings do.

```bash
plaudctl index                              # embed transcripts (idempotent)
plaudctl search "feeling underpaid at work"
```

Or the **Search** tab in the console, where every hit opens the recording cued to the
moment it was said.

Indexing runs as part of `plaudctl run`. On a ~20-hour archive it is ~700 passages and
takes about **14 seconds**; search itself is one embedding call plus a matrix multiply.

Deliberately brute force. ~700 dot products against a 768-dimension vector is well under
a millisecond in numpy — far below the cost of the single network call that embeds your
query. A vector database would add a dependency, a daemon, and an index to corrupt, in
exchange for nothing measurable at this scale. Vectors live as raw float32 in the same
`manifest.sqlite` as everything else, so the archive stays one directory you can copy,
and the index is rebuildable from transcripts at any time.

Embeddings always go through **Ollama**, even if you point the chat model at a hosted
API. Indexing sends every sentence you have ever recorded, which is a far larger
disclosure than summarizing one file, and it should not silently inherit that setting.

Recordings tiered `exclude` are left out, same as everywhere else, with a checkbox to
include them. No single recording can take more than three slots on a page of results.

**On the scores:** they are raw cosine similarity, not confidence. There is no value
below which a result is "wrong" — this model puts most unrelated English text around
0.3–0.5, so a top hit at 0.55 may still be the best the archive has. Compare hits to
each other, and expect quality to fall off after the first few. The console shows the
number rather than hiding it behind a verdict.

`nomic-embed-text` is used with its documented `search_query:` / `search_document:`
prefixes. Honest caveat: on a 4-query hand-built evaluation those prefixes improved mean
rank (13 vs 15) but *reduced* top-3 hits (2/4 vs 3/4) — too small a sample to conclude
anything. They are kept because they are the model's documented usage and Ollama's
template (`{{ .Prompt }}`) confirms it does not add them itself.

## Tone, and the trend

Every transcript is scored for emotional register as part of a normal run — on by
default, no flag. A single reading is close to worthless; a year of them is not, and
the **Trends** tab is where that pays off.

Each recording is scored in segments and reduced, so a two-hour conversation that
turned partway through registers as `mixed` rather than averaging out to a bland
neutral. Three numbers come back:

| | |
|---|---|
| **valence** | −1 hostile or distressed · 0 neutral · +1 warm |
| **energy** | 0 flat · 1 heated — independent of valence. An argument and a celebration are both high energy. |
| **confidence** | the model's own estimate of whether this reading is worth anything |

The third matters most. This is a language model reading *automatic speech
recognition*, which drops words, mangles names, and carries no tone of voice at all.
The prompt asks for a low number when the text is thin or garbled, readings below the
floor are drawn as hollow dots and left out of the trend line, and the neutral band is
deliberately wide so ASR noise doesn't get promoted into a mood. Recordings under ~400
characters of speech aren't scored at all — they're marked as looked-at and left alone.

None of this is a measurement of how anyone felt. Treat a single reading as a prompt to
go and listen to that recording. The trend is the part worth reading.

Recordings tiered **exclude** are left off the chart entirely, the same way they are
kept out of the `stack/` corpus — tiering is physical here too. Without that, the
vendor's own demo files and your misfires sit in the trend reporting their mood as
yours. A checkbox folds them back in when you want to see everything.

The chart is a diverging scale around a zero baseline: two hues that read as opposite
with a neutral gray midpoint, held ~12–16 ΔE apart under simulated protanopia and
deuteranopia. Position on the axis already carries the value, the two extremes are
directly labeled, and a table view carries every number with no colour dependency at
all.

Readings land in the note frontmatter too (`sentiment`, `sentiment_valence`,
`sentiment_energy`, `sentiment_confidence`), so a vault query can reach them — always
with the confidence beside the score.

## Is the vault up to date?

A pill in the console header answers it, and `plaudctl fresh` answers it from a
terminal. "Up to date" is not one fact — it fails in several independent ways, each of
which looks healthy from every angle except the one that catches it:

- recordings sitting in Plaud's cloud that never reached this disk
- audio downloaded but never transcribed, summarized, scored or scanned for actions
- notes the manifest records that no longer exist in your vault — deleted in Obsidian,
  or the vault moved. Without this check, `note_path` being set freezes a stale note
  forever, because nothing would ever rewrite it.
- notes written *before* their tone was scored, and so missing it
- a `stack/` corpus that has drifted from your triage decisions

All of them are checked, and the verdict is clean only when every one is. Untriaged
recordings and unreviewed proposals are reported separately and never count against it:
that is work waiting on *you*, and an indicator that turns amber because you have
reading to do is one you'd learn to ignore.

The cloud check costs a network call and a live session, so it is opt-in — `--cloud`
on the CLI, a button in the console — and a laptop that is offline reports on its own
disk rather than erroring out.

```bash
plaudctl fresh            # local only, exits non-zero if work is outstanding
plaudctl fresh --cloud    # also ask Plaud what it is holding
```

## Measures

| Measure | Question |
|---|---|
| Completion rate, cycle time | Of what you committed to, what got done — and how fast? |
| Acceptance rate | How much of what was proposed was worth keeping? |
| Outcome score vs. intent | Did completed actions produce the intended result? |
| Intent coverage | How much completed work can even be judged? |
| System adherence | For recurring practices, are you actually keeping them up? |
| Conversion rate | What share of recordings became anything at all? |
| Capture-to-decision latency | Is the recorder earning its keep, or just accumulating audio? |

All of it is computed from an append-only event journal, so history survives edits.
Where there isn't enough data to say something honest, the console shows `—` rather
than a flattering zero.

## Deleting from Plaud's cloud

The only destructive verb, and deliberately hard to fire.

```bash
plaudctl prune --probe --yes   # verify the endpoint on ONE recording, first time only
plaudctl prune                 # dry run
plaudctl prune --yes           # send it
```

Plaud's delete endpoint is **inferred** from the API's `is_trash` field, not
documented. Bulk pruning stays locked until a probe run trashes a single recording and
confirms via the API that it actually moved. The proof is written to
`prune-probe-receipt.json` in the archive root; delete the receipt and you're locked
again.

A recording is eligible only if **all** of these hold, re-checked at prune time:

- explicitly marked for deletion in the console — nothing is prunable by default
- its download was confirmed complete (not truncated)
- its sha256 still matches what was recorded at download
- transcribed locally
- has a note, if a notes folder is configured
- older than `prune_min_age_days` (default 14)

Pruning uses Plaud's *trash*, not hard delete, so recordings stay recoverable in their
app for its retention window.

### How completeness is judged

Plaud serves two different things from the same endpoint, and telling them apart took
some doing:

- Once a recording is **transcoded**, you get a real MP3 carrying a 512–640 byte ID3
  tag. Its md5 will *not* match Plaud's `file_md5`, because that hash describes the
  original on-device file.
- Before transcoding finishes, the `.opus` URL returns the **raw on-device blob**
  (starts with `0xB8 0x60`). It is not Ogg/Opus, no decoder will touch it — and its
  md5 matches `file_md5` exactly, because it *is* the original.

So "md5 matched" and "usable audio" are unrelated properties, and the naive check gets
it exactly backwards: the byte-identical files are the ones you cannot use.

Three checks are therefore applied, and a recording must pass all of them before it is
considered archived:

1. **Size** — shorter than Plaud reports means a truncated download, and is refused.
2. **Container** — the magic bytes must be a format a decoder recognizes. Anything
   else means Plaud is still transcoding, so the file is kept but not counted, and the
   next sync retries it.
3. **sha256** — recorded at download and re-checked by `plaudctl verify`, which is
   what actually catches bitrot.

## Concurrency

`plaudctl run` takes an advisory lock in the archive directory. A scheduled sync that
collides with one you triggered from the console exits quietly rather than running
alongside it — two processes writing the same SQLite manifest produced real corruption
in testing. The manifest also runs in WAL mode so the console stays readable while a
run is writing. A lock whose process is gone is treated as stale and taken over, so a
killed run can't wedge the pipeline.

## Known gaps

- **No speaker diarization.** Plaud labels speakers; local Whisper doesn't. Adding
  `pyannote.audio` would close this at the cost of a gated-model login.
- **Local ASR differs from Plaud's.** In testing, `whisper-large-v3-turbo` caught a
  90-second stretch Plaud's own transcript dropped entirely, but it garbles some
  crosstalk. Plaud's transcript is kept in `meta/<id>.json` so you have both.
- **Extraction quality tracks your model.** A small local model will miss
  softly-worded commitments. The transcript is right there; don't treat the action
  list as exhaustive.
- **Tone scoring is an estimate, and the same caveat is sharper.** A transcript has no
  tone of voice in it, so sarcasm, warmth and a calm discussion of something painful
  all read the same on the page. The confidence number is the model's own and is not
  calibrated against anything. Read the trend, not the point.
- **Scoring costs a model pass per recording.** It roughly doubles the LLM work in a
  run, since it chunks the same way summarizing does. On a slow local model a large
  backfill is an overnight job — `plaudctl sentiment --limit N` to do it in bites.
- **Plaud transcodes asynchronously.** A recording synced within minutes of being
  uploaded may arrive as the raw device blob. It's detected, kept, and retried on the
  next sync; nothing is lost, but a very fresh recording may take a cycle to become
  transcribable.
- **Only the Apple Silicon path is battle-tested.** The `faster-whisper`, OpenAI-API,
  and systemd paths are implemented but have not been run on their target platforms.
  Reports welcome.
- **The archive must be on a mounted volume.** If an external drive is unplugged,
  every command fails fast rather than writing a phantom archive to the boot disk that
  would be shadowed on remount.

## Credits

Plaud's API is private and undocumented. The endpoint surface used here
(`/auth/otp-send-code`, `/auth/otp-login`, `/file/simple/web`, `/file/temp-url/{id}`,
`/file/detail/{id}`, and workspace-token minting) was derived by reading
[Riffado / openplaud](https://github.com/openplaud/openplaud), which did the
reverse-engineering. No code was copied, but the knowledge came from there, and this
project is AGPL-3.0 in keeping with that lineage.

Not affiliated with, endorsed by, or supported by Plaud. "Plaud" is their trademark.
Using this may violate their terms of service; that is your call to make.

## License

AGPL-3.0. See [LICENSE](LICENSE).
