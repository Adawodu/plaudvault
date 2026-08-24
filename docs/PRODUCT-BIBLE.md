# plaudvault — product bible

The reference document for what this is, why it is built the way it is, what is done,
and what is next. Everything in here is meant to survive the conversation that produced
it.

> **Maintenance:** the *Status*, *Shipped*, and *Metrics* sections are regenerated from
> the repository and the live archive by `scripts/sync-docs.py`. Everything else —
> decisions, backlog, roadmap — is written by hand and only ever appended to. See
> [Keeping this current](#keeping-this-current).

---

## 1. What this is

Plaud sells a wearable recorder whose audio can only leave the device via their app and
their cloud. They charge for **transcription minutes**, not storage — storage is free
and unlimited. That pricing is the seam this product lives in.

plaudvault lets their cloud be a sync pipe and nothing more. Audio is pulled down over
their API, verified, and then transcribed, summarized, tone-scored, mined for
commitments and indexed for search **entirely on your own machine**. Their AI never
touches the audio, so the paid minutes stay unspent and the transcript never leaves
127.0.0.1.

**What it explicitly does not do:** get audio off the device without their cloud. Plaud
disabled raw USB access in firmware 2.1 and the pin models never had it. "Private" here
means *your storage, your transcription, your summaries, your search, and
delete-after-archive* — not never-touches-their-servers. If that distinction matters, it
should, and no amount of software on this end changes it.

### Who it is for

One person, on their own machine, with their own recordings. There is no multi-user
story, no hosted mode, and no auth on the console — because it binds to loopback and
serves family conversations off a local disk. Every design decision below assumes that.

---

## 2. The three ideas that everything else follows from

**1. Their cloud is a pipe; understanding is local.** Every stage that interprets your
speech — ASR, summary, tone, commitments, embeddings — runs against a model on your
machine. The only network calls are: authenticate, list, download, and (deliberately,
manually) delete.

**2. Tiering is physical, not advisory.** A recording tiered `stack` is *copied* into
`stack/`, the only directory a knowledge index is pointed at. Untier it and the copy is
deleted on the next run. A tier stored only in a database is a promise; a tier enforced
by what exists on disk is a fact. This matters because a pin records whoever is in
earshot, and none of them opted in — defaulting personal recordings to `local` is the
difference between an archive and a surveillance corpus.

**3. The machine proposes; the human decides.** Extraction, tone scores and search
results are all *suggestions*. Nothing reaches a board, a corpus, or a deletion queue
without a human acting. Where the machine cannot be trusted, the design makes that
visible rather than hiding it behind a confident interface.

---

## 3. Architecture

![architecture](diagrams/architecture.png)

`plaudctl run` executes six stages under a single advisory lock:

```
sync → transcribe → summarize → sentiment → notes → extract → index → tier
```

| Stage | Engine | Produces |
|---|---|---|
| `sync` | Plaud API + httpx | `audio/YYYY/MM/*.mp3`, verification facts |
| `transcribe` | mlx-whisper (Apple GPU) / faster-whisper | `transcripts/*.txt` |
| `summarize` | qwen3:8b via Ollama | `summaries/*.md` |
| `sentiment` | qwen3:8b via Ollama | `sentiment` rows |
| `notes` | — | one Obsidian note per recording |
| `extract` | qwen3:8b via Ollama | `actions` rows (proposed) |
| `index` | nomic-embed-text via Ollama | `chunks` rows + vectors |
| `tier` | — | reconciles `stack/` with triage |

### Data model

![data model](diagrams/data-model.png)

### User journeys

![user journeys](diagrams/user-journeys.png)

---

## 4. Technical decisions

Each entry is a decision that was genuinely contested, with the reasoning that settled
it. Superseded decisions are struck through rather than deleted.

### D1 — Judge download completeness by size, not by md5
Plaud's `file_md5` describes the *original on-device file*. The MP3 their storage layer
serves usually carries a 512–640 byte ID3 tag, so the md5 matches only for the minority
served untouched. Treating a mismatch as corruption would condemn most of the archive to
being un-prunable forever.

Worse, the naive check gets it *backwards*: before transcoding finishes, the `.opus` URL
returns the raw on-device blob (starts `0xB8 0x60`), which no decoder will touch — and
whose md5 matches exactly, because it *is* the original. "md5 matched" and "usable
audio" are unrelated properties.

**Decided:** three independent checks — size (short = truncated, refuse), container
magic bytes (undecodable = still transcoding, keep but don't count), and sha256 recorded
at download and re-verified by `plaudctl verify`.

### D2 — Pruning is locked behind a probe receipt
Plaud's delete endpoint is *inferred* from the `is_trash` field on the listing response,
not documented or observed. Bulk deletion stays locked until a probe run trashes exactly
one recording and confirms via the API that it moved. The proof is written to
`prune-probe-receipt.json`; delete the receipt and you are locked again. No scheduled
job ever prunes.

### D3 — One advisory lock for the pipeline
A scheduled sync four times a day plus a "Sync now" button means two runs overlapping is
a matter of when, not if. Two processes writing the same SQLite manifest produced real,
observed corruption. WAL makes that survivable; the lock makes it not happen. A lock
whose PID is gone is treated as stale, so a killed run cannot wedge the pipeline.

### D4 — Sentiment is scored per segment, then reduced
A two-hour conversation that turned partway through would average out to a bland neutral
under a whole-transcript score. Segments are scored separately and reduced; valences that
straddle zero by more than a threshold produce `mixed` rather than a false neutral.
Verified on real data: two recordings scored −0.07 and −0.06 that a plain mean would have
filed as "neutral" are correctly labelled `mixed`.

### D5 — Confidence is stored with every tone score, and shown
This is a language model reading ASR, which has no tone of voice in it. The prompt asks
for the model's own confidence, low-confidence readings are drawn hollow and excluded
from the trend line by default.
**Observed limitation:** on the real corpus every reading came back 0.80–0.95, so the
mechanism has never actually fired. The confidence channel is compressed and currently
close to uninformative. Recorded rather than hidden.

### D6 — The trend chart uses a diverging teal↔terracotta scale, not green↔red
Green/red is the intuitive choice for good/bad and the worst possible choice for
colour-vision deficiency. The poles used here stay 12.5–15.6 ΔE apart under simulated
protanopia and deuteranopia (validated with a script, not by eye), every step clears 3:1
contrast on both light and dark surfaces, and position on the axis plus a table view
carry the value independently of colour.

### D7 — Brute-force vector search, no vector database
~20 hours of audio is ~700 chunks. 700 dot products against a 768-dim vector is well under
a millisecond in numpy — far below the cost of the single network call that embeds the
query. A vector DB would add a dependency, a daemon, and an index to corrupt in exchange
for nothing measurable. Vectors live as raw float32 in the same SQLite file, so the
archive stays one directory you can copy. *Revisit at ~10⁵ chunks.*

### D8 — Embeddings always go through Ollama, even when the chat model is remote
Indexing sends every sentence you have ever recorded. That is a categorically larger
disclosure than summarizing one file, and it must not silently inherit a setting made
for the latter. `embed_model` is a separate config key with no remote option.

### D9 — Every extracted action's quote is verified against its transcript
Found by reading the board, not the code: two proposals were verbatim copies of the
prompt's own worked example — an action to *"Schedule a review call with Dana"* quoting
*"I need to email Dana to set up the review call"*, when "Dana" appears in none of the 32
recordings. A small model sometimes returns the few-shot example instead of reading the
input.

That is worse than a wrong action. The quote exists so a human can check the action
against the recording, so a fabricated quote defeats the audit it is there to support.

Pure verbatim matching was measured and rejected: on the live 255-item board it would
have discarded 23 sound paraphrases to catch 2 leaks. **Decided:** a quote passes if a
40-character run appears verbatim *or* ≥60% of its content words do. Measured result:
253 kept, exactly 2 dropped.

### D10 — Extract commitments only; suggestions are opt-in
Measured on the real corpus (33 recordings, 19.3h): 198 suggestions against 57 commitments, and the suggestions
were largely not tasks — topic summaries (*"Discuss the app's features"*), things that
had already happened during the call (*"Share the screen to show the app concept"*), and
bare noun phrases (*"Secure and compliant infrastructure for managing IP"*). They were
grounded in the recording, so D9's quote check cannot catch them: the failure is
judgment, not fabrication.

Suggestions are removed from the prompt entirely rather than filtered from the response —
a category that is merely *mentioned* is one the model will populate. A backstop drops
any the model volunteers anyway, and says so.

### D11 — The freshness verdict ignores work waiting on the human
Untriaged recordings and unreviewed proposals are reported but never counted against
"up to date". An indicator that turns amber because you have reading to do is one you
learn to ignore. The verdict covers only what the *machine* owes.

### D12 — Prev/next captures a snapshot of the list, not a live query
Triaging a recording removes it from the live Inbox query. A list that re-derived itself
between steps would shift under you mid-pass — you triage one, land two further on, and
never see the one in between.

### D13 — `service install` verifies with launchd rather than trusting `bootstrap`
Observed: the sync agent reported as installed while `launchctl print` could not find it,
because every launchctl call went through a helper that captured output and discarded the
return code. A bootstrap that lost the race to its own plist write left a "scheduled" sync
that would never have run — and you would only discover it when recordings quietly stopped
syncing.

### D14 — qwen3:8b, not the largest model available
`qwen3.8` (27.3B, Q4_K_M, 17.7 GB) was pulled and **cannot load** on a 24 GB machine:
5m04s of thrashing, swap climbing, then `timed out waiting for llama-server to start`,
with the daemon dying. The weights alone are 94% of non-wired RAM before any KV cache.
It is already at the standard 4-bit quant, so there is no smaller variant of that tag,
and the failure happened at `num_ctx: 8192` — the smallest plaudvault ever requests —
so context length was never the constraint.

qwen3:8b does 22 recordings in 14 minutes with zero failures. **Corollary:** score the
whole corpus with one model. The `sentiment.model` column makes a switch traceable, but
a mid-corpus change puts a seam in the trend that looks like a mood shift and is not.

---

## 5. Status

<!-- BEGIN:STATUS (generated — do not edit by hand) -->
_Generated 2026-08-23 from git and the live archive._

### Codebase

| | |
|---|---|
| Python modules | 22 |
| Lines of Python | 4,914 |
| Commits | 7 |
| CLI verbs | 19 — `login`, `logout`, `status`, `fresh`, `sync`, `verify`, `index`, `search`, `tier`, `web`, `init`, `service`, `run`, `prune`, `transcribe`, `summarize`, `sentiment`, `notes`, `extract` |

Largest modules: `store.py` (508), `web.py` (444), `cli.py` (398), `metrics.py` (320), `service.py` (283), `extract.py` (264).

### Live archive

| | |
|---|---|
| Recordings | 33 |
| Transcribed | 33 |
| Tone scored | 32 |
| Indexed chunks | 716 |
| Triaged | 10 |
| Open commitments | 81 |
| Action events | 528 |
| Audio captured | 19.3 hours |
| Tiers | local 3 · stack 7 |

<!-- END:STATUS -->

---

## 6. Shipped

<!-- BEGIN:SHIPPED (generated — do not edit by hand) -->
Newest first. Each commit message carries the reasoning; this is only the index.

| Date | Commit | What landed |
|---|---|---|
| 2026-08-23 | `7202bfa` | Document the whole thing: three diagrams, a product bible, and a way to keep it current |
| 2026-08-22 | `2470d05` | Step between recordings without going back to the list |
| 2026-08-22 | `d37c4ac` | Semantic search over transcripts, with timestamped hits |
| 2026-08-22 | `34d82c2` | Extract commitments only; suggestions become opt-in |
| 2026-08-22 | `6437eef` | Refuse extracted actions whose quote isn't in the transcript |
| 2026-08-21 | `e679a3b` | Keep excluded recordings off the trend, and verify launchd actually loaded |
| 2026-08-21 | `0f72395` | Own your Plaud recordings end to end |
<!-- END:SHIPPED -->

---

## 7. Backlog

Ordered within each tier by expected value, not effort. Nothing here is committed.

### Next — clear value, design settled

| # | Item | Why |
|---|---|---|
| B1 | **Ask-with-citations over the index** (`plaudctl ask`, `/api/ask`, console tab) | The retrieval half already exists. Answers must carry per-claim citations to recording + timestamp, so the answer is an index *into* evidence, never a replacement for it. |
| B2 | **MCP tool exposing search + ask** | Makes plaudvault a retrieval service the Cognitive Stack calls, with tiering enforced at the single source. The stack never holds a copy of the vectors. See §9. |
| B3 | **Retrieval evaluation harness** | Everything above rests on retrieval quality nobody has measured properly. A labelled query set, measured rank, run on every change. Also settles the open prefix question (D-open-1). |
| B4 | **Neighbour expansion for answer context** | Chunks are 1200 chars, tuned for search snippets. RAG wants the hit plus its neighbours. |
| B5 | **Date/tier filters ahead of vector search** | "What did I commit to last week" is a metadata question. Similarity alone cannot answer it. |

### Later — valuable, design not settled

| # | Item | Open question |
|---|---|---|
| B6 | **Topic trends over time** | Aggregation, not retrieval — clustering or per-period map-reduce. "How has my thinking on X changed" is a different build from RAG. |
| B7 | **Speaker diarization** | `pyannote.audio` closes it at the cost of a gated-model login. Would materially improve extraction (who committed?) and tone. |
| B8 | **Better commitment precision** | 63→18 proposals after D10, but roughly half the survivors are rhetorical ("Make this a reality"). Rhetoric and commitment are grammatically identical to an 8B model. Needs a larger model or a second-pass judge — both were rejected once already. |
| B9 | **Backup/restore command** | The precious/derived split in the data-model diagram is the spec. Nobody has written the command. |
| B10 | **Notification when a scheduled run fails** | A 07:00 sync with the drive unmounted is a silent no-op. Freshness surfaces it only if you look. |

### Rejected — recorded so they are not re-proposed

| Item | Why not |
|---|---|
| Vector database (Chroma, sqlite-vec, …) | D7. Revisit at ~10⁵ chunks. |
| `qwen3.8` / larger local model | D14. Physically cannot load on 24 GB. |
| Filtering suggestions out of the LLM response | D10. Asking and discarding still spends the model's attention inventing them. |
| Pure verbatim quote matching | D9. Measured: would discard 23 sound actions to catch 2 bad ones. |
| Exposing the console beyond loopback | No auth by design. Would need a real identity proxy first. |

---

## 8. Roadmap

**Now — trustworthy recall.** B3 first (measure what we have), then B1 and B4. The
sequencing is deliberate: building answers on unmeasured retrieval is how you get a
system that sounds authoritative and is wrong.

**Next — the stack integration.** B2. Once ask-with-citations exists locally, exposing it
as a tool is small, and it settles the boundary question in §9 permanently.

**Then — extraction quality, or retire the ambition.** B8 is the weakest part of the
product. Either it gets materially better, or the honest move is to reframe the Actions
board as "moments worth revisiting" rather than a task list.

**Ongoing — the corpus grows.** Everything above assumes ~20 hours. At 200 hours,
revisit D7 (brute force), chunk sizing, and whether `plaudctl run` still fits in a
scheduled window.

---

## 9. Boundary with the Cognitive Stack

A recurring question, settled here so it is not re-litigated.

**Retrieval belongs in plaudvault. Synthesis may live in either. The stack should call
plaudvault rather than re-index.**

1. **Tiering is a safety property and needs one enforcement point.** plaudvault owns the
   only index that knows a recording is `local` or `exclude`. Two systems independently
   deciding what is private will drift, and the drift is silent.
2. **plaudvault can cite what the stack cannot reconstruct** — timestamp, audio
   deep-link, tier, tone, extracted commitments. Once transcripts are flattened into
   `stack/*.txt`, all of that is gone.
3. **The stack's job is cross-source synthesis** — repos, notes, MS365, recordings. It
   should ask plaudvault *"what do the recordings say about X"* and combine that with
   other sources.

Note the corpora differ and always will: the stack sees only `stack`-tiered recordings
(7 of 32 at time of writing), by design.

---

## 10. Known limits

Stated plainly because each one is a way this product can mislead you.

- **Tone scores are estimates over ASR.** A transcript has no tone of voice, so sarcasm,
  warmth, and a calm discussion of something painful all read the same on the page.
- **Confidence is currently uninformative** (D5). Treat valence as the useful number.
- **Extraction still surfaces rhetoric as commitment** (B8). Treat the board as prompts
  to check recordings, not a task list.
- **Extraction is non-deterministic.** Two runs over the same transcript returned
  different commitments. Re-running `--force` gives a different board.
- **Search scores are cosine similarity, not confidence.** Unrelated English sits around
  0.3–0.5; a top hit at 0.55 may still be the best the archive has. Quality falls off
  after the first few hits.
- **Local ASR differs from Plaud's.** `whisper-large-v3-turbo` caught a 90-second stretch
  Plaud's own transcript dropped entirely, but garbles some crosstalk. Theirs is kept in
  `meta/<id>.json` so you have both.
- **Plaud transcodes asynchronously.** A recording synced minutes after upload may arrive
  as the raw device blob. Detected, kept, retried next run.
- **Everything depends on one external volume.** Archive *and* models live on it. It
  unmounted once during development: the failure is graceful and self-recovering, but a
  scheduled run with the drive detached is a silent no-op (B10).
- **Only the Apple Silicon path is battle-tested.** faster-whisper, OpenAI-API and
  systemd paths are implemented and unrun on their target platforms.

---

## 11. Open questions

| # | Question | Status |
|---|---|---|
| D-open-1 | Do `search_query:`/`search_document:` prefixes actually help retrieval here? | A 4-query eval was inconclusive — better mean rank (13 vs 15), worse top-3 (2/4 vs 3/4). Kept because they are the model's documented usage and Ollama's template (`{{ .Prompt }}`) confirms it does not add them itself. Needs B3. |
| D-open-2 | Should the Actions board stay a task list? | Depends on B8. |
| D-open-3 | Is a 1200-char chunk right for both search snippets and answer context? | Probably not. See B4. |

---

## Keeping this current

`scripts/sync-docs.py` regenerates the **Status**, **Shipped**, and **Metrics** blocks
from git history and the live manifest. It only ever rewrites text between
`<!-- BEGIN:X -->` and `<!-- END:X -->` markers, so hand-written sections are never
touched.

```bash
python scripts/sync-docs.py          # rewrite the generated blocks
python scripts/sync-docs.py --check  # exit 1 if stale (for CI or a hook)
```

A git `post-commit` hook runs it automatically after every commit —
see `scripts/install-hooks.sh`.

**What it cannot do:** decide that a decision was made, or that an item moved from
backlog to shipped in spirit rather than in commits. Sections 4, 7, 8, 9, 10 and 11 are
written by hand. When we finish a piece of work, the decision that drove it gets an entry
in §4 and the backlog item is struck from §7 — that part is a habit, not a script.
