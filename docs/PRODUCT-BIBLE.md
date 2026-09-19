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
sync → transcribe → diarize → summarize → title → sentiment → notes → extract → index → tier → browse
```

| Stage | Engine | Produces |
|---|---|---|
| `sync` | Plaud API + httpx | `audio/YYYY/MM/*.mp3`, verification facts |
| `transcribe` | mlx-whisper (Apple GPU) / faster-whisper | `transcripts/*.txt` |
| `diarize` | pyannote (optional) | `diarization/*.json`, `recording_speakers` rows, named transcripts |
| `summarize` | qwen3:8b via Ollama | `summaries/*.md` |
| `title` | qwen3:8b via Ollama | `recordings.title` |
| `sentiment` | qwen3:8b via Ollama | `sentiment` rows |
| `notes` | — | one Obsidian note per recording |
| `extract` | qwen3:8b via Ollama | `actions` rows (proposed) |
| `index` | nomic-embed-text via Ollama | `chunks` rows + vectors |
| `tier` | — | reconciles `stack/` with triage |
| `browse` | — | reconciles `by-name/` — readable links over the hash-named archive |

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

### D15 — `exclude` means out of the console *and* out of the pipeline
The console is a workspace, so noise you have already judged as noise must stop asking
for attention. `exclude` was half-built: it dropped a recording from Trends, Search and
`stack/` but left it in the Library, and the pipeline kept summarizing, tone-scoring,
mining and embedding it — spending model time on material already declared worthless.

**Decided:** one predicate, `Store.NOT_EXCLUDED`, spelled once and used by every
"what still needs doing" query, so the console and the pipeline can never disagree
about what counts.

Freshness had to follow, or the pill would sit amber forever over work nobody wants
done — the same cry-wolf failure D11 exists to prevent.

Three properties make dismissing safe enough to do on one click with no dialog:
- **Nothing is deleted.** Audio, sha256, size and container facts are untouched, so the
  recording stays verifiable and prunable later.
- **It is permanent.** Triage lives in its own table; `upsert_remote` writes only
  `filename`, `remote_md5`, `remote_size` and `meta_json`, so a re-sync refreshes
  metadata and leaves the decision alone. Verified by a test that re-lists every
  recording with changed names and asserts the dismissal survives.
- **It is reversible and visible.** A `show dismissed` toggle restores them, and the
  Library always prints the hidden count — quiet by default must never become silently
  missing.

### D16 — Deleting from Plaud must never reduce the local archive
The archive is the copy of record; the cloud is a pipe. Deleting a recording in Plaud's
app — rather than via `plaudctl prune` — is a legitimate workflow, and the Plaud account
may legitimately end up empty.

`sync` only ever adds. It never deletes a local row or file because something vanished
from the listing. Proven by a test that syncs against a client returning zero
recordings and asserts all local rows and audio files survive.

### D17 — A title is written from the summary, and a human's is never overwritten
Plaud names a file after the clock. That is fine for a filesystem and useless for an
inbox: thirty rows of timestamps tell you nothing about which one was the call with the
lawyer.

Titles are generated from the **summary**, not the transcript, because the summary has
already done the map-reduce over a long conversation and its Key points are a far better
title source than the first 8k characters of raw ASR. Recordings under
`summarize_min_seconds` never get summarized, and those are exactly the voice memos whose
timestamp tells you least — so they fall back to the transcript.

Two guards, both measured against the failure they exist to prevent:
- **`title_source` records who wrote it.** `--force` re-titles the machine's own work and
  never yours, the same rule triage lives by. Clearing the title in the console is how you
  say "your title was wrong, try again".
- **A title that names nothing is refused.** A model that cannot find a subject reaches
  for "Business Discussion", and thirty of those are no better than thirty timestamps. If
  every word of the proposal is generic the recording stays unnamed, which is honest.
  Measured on the live corpus: 49 of 50 named, 1 correctly declined.

The device's filename is shown alongside the title everywhere, so a title you disagree
with never hides what the file actually is.

### D18 — Only a human confirmation builds a voiceprint
Diarization produces anonymous labels — `SPEAKER_00` — which are per-recording and
useless across an archive, because `SPEAKER_00` is a different person in every file. The
value is entirely in the identity laid over them: name a voice once, keep its embedding
as a **voiceprint**, and the next recording matches by voice rather than asking again.

The obvious implementation feeds every match back into the mean, and it is wrong. An
automatic match that is treated as evidence compounds: the identity slowly becomes
whoever the machine has been mistaking for you, and nothing in the data says when it went
wrong. So `source` distinguishes `human` from `voiceprint`, only `human` rows build the
mean, and the console draws a machine match as a visible guess rather than as a fact.

Three properties follow, each covered by a test:
- **Re-running diarization never un-names anybody.** Attribution lives in its own column,
  exactly as triage survives a re-sync.
- **A correction reaches the identity, not just the label.** Taking back an attribution
  rebuilds that person's voiceprint without it, so a mistake does not stay baked in.
- **The rendered transcript is derived, never authoritative.** Names are re-applied from
  the database on demand, so a rename rewrites every transcript that person appears in.

The mean is weighted by speaking time: a thirty-second cameo should not move an identity
as far as an hour of conversation. Degenerate embeddings are dropped — pyannote pads
under-sampled clusters with zeros, and a zero vector matches everything at cosine 0 and
nothing usefully.

### D22 — Decode for pyannote with PyAV, not with its own loader
pyannote 4 reads audio through `torchcodec`, which links against FFmpeg's shared
libraries. On a machine with no system FFmpeg it fails with `Library not loaded:
libavutil.56` — and this machine deliberately has none, because transcription already
decodes in-process with PyAV precisely to avoid that dependency (and the broken Homebrew
ffmpeg that comes with it).

Installing FFmpeg to satisfy a transitive C++ dependency would have undone a decision the
project already made. Instead the pipeline is handed a waveform: `load_audio` — the same
16 kHz mono decode the transcript came from — produces a tensor that pyannote accepts in
place of a path. Two things follow: there is still no system dependency to install, and
diarization and transcription now see byte-identical samples, so their timestamps cannot
drift apart because two decoders disagreed.

### D23 — A voice must speak for 30 seconds before you are asked to name it
Found by running on the real corpus rather than by reasoning: a pin worn through a family
shopping trip produced **eight** voices, three of them under half a minute — the
shopkeeper, a passer-by, a child three aisles away. All of them real, none of them people
anyone will ever name.

Left unfiltered they dominate the naming queue by count, and a work list mostly full of
items you will never action is a list you stop opening. That is the same failure D11 and
D15 exist to prevent, arriving through a different door.

`speaker_min_seconds` (default 30) filters the *queue* and the freshness count. It does
not filter the recording: every voice still appears in that recording's speaker table and
can still be named there, `--all` shows them, and the hidden count is always printed —
because quiet by default must never become silently missing, which is the same guarantee
`exclude` carries in the Library.

### D19 — Diarization is optional, and its absence must not turn the pill amber
pyannote pulls ~2 GB of torch and needs a HuggingFace licence accepted for two gated
models. That is a real cost to impose on somebody who only wants transcripts, so it is an
extra (`pip install 'plaudvault[speakers]'`) and every other stage works without it.

The consequence that matters is in freshness: if undiarized recordings counted as
outstanding work on a machine with no token, the indicator would sit amber forever over
work nobody can do — the exact cry-wolf failure D11 and D15 exist to prevent. So the
diarize stage reports zero pending unless diarization is actually available, and
`plaudctl speakers status` says precisely what is missing and which licence page to open.

### D20 — Only an accepted action can be dispatched, and a report is not a completion
Handing an action to an agent points something that can act in the world at a sentence a
small model extracted from noisy ASR of a family conversation. Three constraints, and
none of them has an override flag:

1. **Only `accepted` (or `in_progress`) can be handed over.** `proposed` is the
   extractor's guess, and D10/B8 measure it as over-proposing. Requiring acceptance means
   a human read the quote before anything could act on it.
2. **Dispatch is a request, never an execution.** plaudvault writes a row and waits.
   Whatever the agent can do, it could already do; this only tells it what you want, so
   the blast radius is the agent's and not the archive's.
3. **A finished job is a report.** The result lands on the dispatch row and the action
   stays open. An agent that believes it booked a meeting and did not must not be able to
   tick the box itself.

The quote and the recording travel with the job, because an agent told to "set up the
meeting" with no source cannot tell a real commitment from a garbled one — the same
reason D9's quote verification exists. Claiming is atomic (the status guard is in the
`UPDATE`, not a read-then-write), so two agents polling one queue cannot both believe
they won.

### D21 — The MCP server sees everything except `exclude`, not only `stack`
§9 settles that the *cognitive stack* sees `stack`-tiered recordings only, and that has
not changed. This is a different client: an MCP server on stdio, launched by the owner's
own agent on the owner's own machine, which is nearer to the console than to a corpus
crossing a boundary. Scoping it to `stack` would have made it useful for 7 of 57
recordings and answered almost nothing.

So `mcp_tier_scope` defaults to `stack,local,untriaged` — what the console shows. Three
things keep that from being a quiet widening of the archive's blast radius:
- **`exclude` is unreachable through every path** and is not expressible in the scope.
- **Audio is never served.** The transcript is the surface.
- **The scope is per-invocation.** `plaudctl mcp --tiers stack` hands a particular client
  a narrower view than the console has, so a client that should not see family
  conversations does not, without changing the config for the others.

Tier is still enforced in exactly one place. What changed is the default, and it is
recorded here because the reasoning is the sort that gets re-litigated.

### D24 — The archive is browsable through links, and is never renamed
Every file on disk is named for its Plaud recording id, which is correct and unreadable:
opening the archive in Finder shows sixty identical hashes. The obvious fix — name the
file after its title — is the wrong one, for three reasons that compound.

A title is a *proposal*. `plaudctl title` rewrites it whenever the model is re-run, and
you can rewrite it by hand. Naming the file after it means the audio moves every time a
guess changes. That audio is the one artifact in the system that cannot be regenerated,
and every stored path, Obsidian note link and Finder alias pointing at it breaks on each
move. Five recordings currently have no title at all, so the scheme cannot even be
applied uniformly. And the id is the join key for the entire pipeline; making a mutable,
model-authored string load-bearing for file identity inverts which of the two is stable.

So `PLAUD/by-name/` is a *view*: symlinks named `2026-08-31 1202 · 174m · Metric Health
SOC2 Compliance Audit.mp3`, grouped by artifact kind, rebuilt at the end of every run
once the titler has settled. Date first so it sorts the way you look; duration next
because it separates a voice memo from a real conversation at a glance. The tree is
disposable — delete it, rebuild it, nothing is lost — and reconciliation removes stale
links exactly as `tiering.py` removes stale copies, so untiering a recording takes it out
of the browsable directory too.

**Links here, copies in `stack/` — the opposite of D-tiering, for the opposite reason.**
`stack/` is read by an indexer that may or may not follow symlinks, and a link followed
into the full corpus is a privacy failure. Nothing reads `by-name/`; a person does. The
risk runs the other way, so the answer does too.

Reconciliation only ever unlinks *symlinks*. A real file that somehow lands in the tree
is left alone, because this runs unattended over an external drive and losing a file to a
tidy-up must not be a reachable outcome.

### D25 — You hear a voice before you name it, and a sample is an offset not a file
Naming a voice you have never heard is guesswork, and a guess here is expensive: a
confirmed name becomes a voiceprint, and that voiceprint attributes speech in every later
recording. Name the wrong person once and the archive agrees with you from then on, with
nothing in the data saying when it went wrong (the same failure D18 refuses to create
automatically). So every place that asks *who is this?* can now play them.

**A sample is a pair of offsets, not a clip.** The console already has the audio and the
browser can seek — `FileResponse` answers a Range request with 206 — so the server returns
`{start, end}` and cuts nothing. That costs no ffmpeg, no disk, no cache invalidation when
diarization is re-run, and it means no loose clip of somebody's voice ever exists as a
file that could be copied somewhere the tier does not follow.

Which seconds are chosen matters more than it looks. The longest turns win, because a long
turn is one person talking rather than two people colliding. Each clip is taken from the
**middle** of its turn, because diarization boundaries are exactly where the model is least
certain and the first and last second are the likeliest to be somebody else. Clips play in
time order rather than longest-first, so a label that is really two people sounds like two
people.

**A voice too fragmented for a clean clip is padded, not refused.** One speaker in the
live archive talks for 109 seconds across 135 turns and never once holds the floor for a
second and a half — every turn is a half-second interjection. The first version returned
nothing for them and the console said *no clean sample* next to somebody plainly audible.
When no turn clears the floor, the longest ones are widened to a four-second window and
flagged `padded`, and the console says *in context* while playing, because you will hear
whoever they are talking over. A window near either edge of the recording slides inward
rather than being trimmed: trimming would give the shortest clip to the person whose only
audible moment is at the end.

**Chips are playable wherever they appear.** Inbox, library, search results. Triage is
the moment you most want this — deciding what a recording is and who is in it is one
decision — and it should not require opening the recording first.

**Naming is inline; the dialog keeps the rest.** A queue of 127 unnamed voices is not
127 dialogs. The common case — a voice you recognise and a name you have already used —
is now type-and-Enter against a datalist of existing people, which reuses that person
rather than creating a second one. The dialog remains for everything that is genuinely a
decision: a contact reference, marking a voice as yourself, un-naming.

That inline path exposed a real bug and it is worth recording. `add_speaker` is
create-or-reuse by name, and its upsert overwrote `is_me` with whatever the caller passed.
The dialog always sent the checkbox so nobody noticed; an inline rename sends only a name,
which would have silently un-me'd you the second time you confirmed your own voice.
`is_me=None` now means *do not touch it*, and only an explicit value changes it.

### D26 — A constraint the caller stated is a filter, never a hint to the ranker
`plaudctl search "what did I commit to in August"` returned, among its top four, a
recording from **September**, and four passages that were not commitments. Nothing was
broken: "August" was embedded as meaning and compared against text, which is all an
unfiltered vector search can do with it. Meanwhile the same question as a query over the
tables — recordings started in August joined to actions of `kind = 'commitment'` —
returns 71 correct rows.

The archive held the answer. Similarity search was the wrong instrument, and it failed
*confidently*, which is the failure the eval harness exists to catch and the one that
matters most now that an MCP client writes prose over these hits.

So `store.chunks()` and `search()` take `since`, `until`, `tiers` and `speaker`, applied
in SQL **before** anything is ranked, and the same filters reach the CLI (`--period`,
`--speaker`) and the MCP tool. Filtering before ranking rather than after also fixes a
quieter bug: post-filtering a top-k list returns three hits when eight were asked for and
gives no way to tell "nothing matched" from "the good matches were filtered out".

`plaudvault/period.py` turns what a person says into a half-open range — `"March"`,
`"March 2026"`, `"2026-03-14"`, `"last 30 days"` — because a tool that only accepts
`YYYY-MM-DD` pushes that conversion onto an agent, where an off-by-one month looks
exactly like an archive with nothing in it. A bare month resolves to the most recent one
that has already started, and **an unparseable period raises rather than defaulting to no
filter**: silently answering a question about March with the whole corpus would look
entirely plausible.

`list_actions` gained `period`, `kind` and `owner`, and each row now carries the quote,
the recording timestamp and a `dispatchable` flag. Its period bounds **when the
commitment was recorded, not when it is due** — only 4 of 681 actions carry a due date,
so due-date filtering would answer a question nobody asked and return nearly nothing
while appearing to work.

### D27 — An API key is not permission; the cloud gets a tier scope
A larger model is the obvious answer to B8, D14 rules out a larger *local* one on 24 GB,
and `llm_provider = "openai"` already targets any OpenAI-compatible endpoint. So the
remaining obstacle to a cloud model was never plumbing — it is that this archive holds
therapy sessions, arguments with a spouse and conversations in front of children beside
compliance meetings, and `is_local()` drove a single global warning that could not tell
them apart.

`cloud_tier_scope` is that distinction, enforced the way D21 enforces the MCP scope.
**It is empty by default**: setting a key opens nothing. `generate()` refuses a tier
outside the scope, and refuses rather than quietly falling back to the local model, since
a silent downgrade produces different output with nothing recording which model wrote it.
An unknown tier is refused too — the stages now pass the recording's tier explicitly, so
"I could not tell" means no.

This landed *before* any key did, which is the only order in which it is worth anything.

### D28 — A search hit stays the citation; context is a second field
A chunk is 1200 characters because a hit has to point at a *findable moment* — small
enough that "somewhere in these ten minutes" is not the answer. That is the wrong size
for the other thing these hits are now used for: an MCP client reading one and writing
prose over it has a paragraph torn out of a page. The manual workaround was a second
`get_transcript` call with hand-guessed bounds, which is a round trip and a guess.

So `search()` takes `context=N` and `search_recordings` defaults it to 1. The tempting
version — widen `passage` itself — was rejected: `passage` sits at `at`, and it is the
text the tool docstring tells a client to quote. Widen it and the client keeps quoting
the field, now attributing a neighbour's sentence to a timestamp where it was never
said. The citation is the product, so the hit is returned exactly as indexed and the
window arrives beside it as `context`, carrying its own `context_span`.

Neighbours are stitched, not concatenated: chunks re-seed from the tail of the one
before, so a naive three-chunk window repeats ~400 characters of real speech — measured
at 420 on a live recording — and a model reading a sentence twice can report it as
emphasis or as two separate moments. The overlap is an exact suffix/prefix run, so it is
matched exactly and only above 24 characters. A missed overlap reads as mild repetition;
a wrongly-guessed one would delete words that were actually said, and only one of those
is recoverable.

Overlapping windows between two hits in the same recording are **not** merged. With the
per-recording cap at 3 they will sometimes duplicate, and the cost is tokens; merging
would hand the client two response shapes for one field, and the cost of that is a model
silently ignoring the pointer form. Revisit if payloads become the complaint.

### D29 — A linter earns its place by finding what a reader cannot
Added ruff and a CI workflow with a deliberately narrow rule set — unused imports,
shadowed names, unsorted imports, obvious bug shapes — because this codebase argues for
itself in prose and a linter with opinions about prose would be noise. The first run
found `sentiment._score_segment` calling `_generate(..., tier=tier)` with **no `tier` in
scope**: a `NameError` on every tone-scoring call, caught by the surrounding
`except Exception` and reported as an ordinary per-recording failure. It arrived with
D27 — the commit that gave the cloud a tier scope threaded `tier` into the call without
adding it to the signature — so tone scoring has been dead since 2026-09-03, with 61 of
94 recordings holding readings taken before it and 33 silently failing since. The broad
except is why nobody ever saw a traceback.

Two things follow. The tier now travels with every segment rather than being read once
in `run()` and dropped — it is the argument the remote-provider gate reads, so losing it
en route is a safety failure, not a cosmetic one. And a broad `except` around a loop is
a legitimate pattern here (one bad recording must not stop a batch), which is exactly
why the static check has to exist: the runtime was designed to keep going, so the only
thing that could notice was a tool that reads the code.

### D30 — A conversation has a kind, and the kind carries a budget
Extraction ran identically on every recording, and the archive says what that cost:
**median 12 actions per conversation against a wanted 2-3, maximum 69, and 567 of 982
dropped by hand.** 539 of those drops arrived in bursts of ten or more — that is not
triage, it is clearing the board, and it means the item-level labels are worth less than
they look while the recording-level verdict is unambiguous: *this conversation should
have produced almost nothing.*

Nothing was broken. `extract_from_text` asks each chunk "what commitments are here?",
a long recording is ~15 chunks, and a leading question gets answered. One prayer session
produced 69 action items. The missing fact is the cheapest one available: **a prayer is
not a standup.**

So one call per *recording*, against the summary that already exists, puts it in a fixed
vocabulary — `working`, `product`, `interview`, `personal`, `devotional`, `media`,
`other` — and each kind carries a budget. Budget 0 means extraction **never runs**, which
is a different outcome from running and finding nothing: the console can say "nobody in
the room was committing to anything" instead of showing an empty board that looks like a
failure.

**`devotional` was budgeted 0 and the archive overruled it.** A sermon on spiritual
leadership produced *"commit to breaking bread with someone in an intentional way at
least once a month"* — which its owner is acting on, and which a budget of 0 would have
hidden. It is 1 now: a teaching conversation is not a working session, but it is not
empty either. `media` stays 0 on firmer ground — nobody in the room is speaking, and no
action from a `media` recording has ever been accepted.

**The vocabulary is fixed, not learned.** A clustering would drift with the corpus and
take the budgets, the console labels and the MCP contract with it. Six kinds a person can
hold in their head is a schema; a clustering is a snapshot. A model that invents a kind
lands on `other` with confidence 0 rather than being coerced to `working` — coercion
would hand a budget of 3 to something nobody classified.

`source` records who decided, and a model run never overwrites a human, for the same
reason a re-titled recording keeps a person's title (D17): the kind decides whether
extraction runs at all, so a silent reclassification changes what reaches the board.

**Measured on the corpus.** 70 of 87 classified (17 had no summary yet): personal 33,
interview 12, product 7, working 7, devotional 6, other 4, media 1. Against today's
980 actions the budgets allow **111** — and 41 of those actions sit in devotional
recordings that will now never be scanned at all.

**The budget is reported, not yet enforced.** Choosing *which* three survive needs a
ranker, and truncating by order of appearance would be an arbitrary answer wearing a
confident face. Until that exists, a run prints how far over budget it went. The gap
between 980 and 111 is the size of the next problem, now stated in a number.

**What this does not fix.** The worst offenders — 69, 63, 61 actions — are single files
holding several unrelated conversations, a pin left running. Their summaries honestly say
"a mix of family, tech and business", so they classify as `other` and keep a budget of 3.
The classifier is right and the *file* is wrong. **B15 (split at conversation boundaries)
is therefore a prerequisite for the budget to bite on exactly the recordings that need it
most**, which was not obvious before this was built.

### D31 — The ranker is a comparative selection call, after a rubric was measured and thrown away
D30 left the budget unspent because choosing *which* three needs a way to compare
candidates. The obvious answer was built first: a lexical rubric scoring concreteness,
intent markers and a time expression, with weights in one place and a breakdown on every
score. It is deleted, and the measurement is why.

Scored against the only ground truth this archive holds — the seven actions its owner
accepted, against the 975 he did not — it put **one of seven** inside its budget. On mean
position within each candidate list (0 = top, 1 = bottom) it scored **0.382, against
0.449 for extraction order and 0.487 for random.** Better than nothing; nowhere near good
enough to pick three.

The reason it failed is worth more than the rubric was. *"Define the compensation band
for the role"* was dropped; *"Identify the target audience for the app"* was kept. Those
are the same sentence — same concreteness, same absent time, same abstract-ish object.
What separates them is whose commitment it is and whether it was decided or merely aired.
**Neither is visible in the sentence**, so no sentence-scorer can find it. A first-person
filter fails the same way and worse: only 3 of the 7 kept actions contain one, against
27% of the 565 dropped.

So selection is one model call per *recording*, with the summary in the prompt and every
candidate visible at once:

1. **Comparative, not absolute.** A small model asked "is this a real commitment?" about
   one sentence is guessing. Asked "which three of these fourteen" it is choosing, which
   is the thing it is good at.
2. **One call.** Extraction already makes ~15 per recording, so precision costs about 7%
   more. The objection that sank a per-item second-pass judge does not apply to a pass
   that runs once.
3. **The budget is an argument, not an inference.** The number is a product decision; the
   model is only asked to honour it.

**Permission to return nothing is the most important line in the prompt.** Extraction
over-produces because it asks each chunk a leading question. Selection is told explicitly
that most conversations hold one or two real commitments and that `{"keep": []}` is a
correct answer.

**Measured on a property the corpus can actually support.** Agreement with seven accepts
is an underpowered test — selection scores 1/7 there too, which at three slots from
pools of 6-69 is indistinguishable from chance. But *meta-talk* — actions that describe
the conversation rather than work arising from it — is measurable at scale, and it is
demonstrably what the owner rejects: `walk` (15), `discuss` (14), `clarify` (11) and
`share` (11) lead the opening verbs of his 565 dropped actions. Across 14 recordings:

| | meta-talk rate | n |
|---|---|---|
| all candidates | 12.0% | 565 |
| first-3 by extraction order | **21.2%** | 33 |
| selection | **0.0%** | 28 |

Extraction order is *worse than the corpus average*, because meta-talk clusters early in
a conversation — which is what "take the first three" was quietly doing. Selection
removed it entirely (0 of 28, p≈0.001 against the baseline rate).

**Nothing is deleted.** Everything not selected becomes `overflow`: kept, off the board,
carrying the model's reason, and promotable in one click. The cut is a judgement about a
dozen sentences, not a fact, and a filter you cannot see is one you stop trusting. A
selection that fails to parse keeps *everything* and says so — `[]` and "no usable
answer" are different answers and must never collapse into each other.

### D32 — Label the pool, not the picks
Two rankers have now been proposed for this budget and neither could be validated,
because the archive's own history cannot support it: seven accepted actions, and 539 of
the 567 drops arriving in bursts of ten or more. A burst is a person clearing a board,
not judging items — so the negatives are unreliable and the positives are seven.

The number has to be made. The design question is *what to label*, and the obvious
answer is wrong: labelling a ranker's picks produces a set that rots the moment the
ranker changes, which is precisely the trap B14 exists to warn about. So `plaudctl judge`
labels **the whole candidate pool** for a sample of recordings. Any future ranker can
then be scored against it offline, forever, without asking a person the same question
twice.

Three properties make it honest. **Order is randomised**, because a list shown in
extraction order anchors the judge on one of the things being measured. **The verdict is
per item**, because "would you put this on your list" is answerable about a sentence and
"is this board good" is not — a board-level verdict is the bulk drop that made the
existing history useless. And **an unlabelled pick counts as nothing, not as a miss**,
because a half-labelled set must not make a working ranker look broken.

`plaudctl judge --measure` reports precision *and* recall of keepers. Precision alone
would make "keep nothing" look perfect, which is the exact failure a budget invites.
The set lives at `{archive_root}/eval/actions.jsonl` — beside the transcripts, never in
this repository, same reasoning as the retrieval golden set.

### D33 — A product conversation earns a brief, not a bigger budget
The owner's exception to a board of three is the conversation that specifies something
for an agent to build, research or execute. The tempting reading is that those deserve a
larger ceiling. That is the wrong shape: sixty checkboxes is not a specification, it is a
specification shredded into sixty pieces that have each lost the context that made them
meaningful.

What an agent needs to start is a **brief** — what is being built and why, the
constraints, what was decided, what is still open — and then a small number of actions,
which selection already produces. So `product` keeps its budget of three and gains a
document beside it, written to `{archive_root}/briefs/`.

**`## Open` is the section that justifies the document.** A model asked to summarise a
design conversation reports the decisions and quietly drops the disagreements, because
decisions sound like conclusions and open questions sound like noise. An agent that acts
on the decisions while unaware of what is unresolved is the specific failure this
prevents, so the section is demanded explicitly, "None stated" has to be written out, and
the merge step that combines partial briefs is told that the only reason to drop an open
question is that a later part of the conversation resolved it — in which case it moves to
`## Decided` rather than disappearing.

**A brief needs a human-confirmed kind, and that gate was learned on the first real
run.** It produced a brief for a recording whose title named two people and a business
topic — classified `product` at 0.95 confidence, because a summary of it genuinely is
business analysis, while the conversation is mostly a personal argument with some
business talk in the middle. A veto in the prompt was tried first and **did not fire**: a
chunk full of real business analysis has no reason to decline, and no chunk can see what
a conversation is *mostly* about. The veto stays as a cheap second line; it is not the
control.

The control is the rule the product already runs on — a proposal does nothing until a
person accepts it. An action must be accepted before it can be dispatched (D20); a brief
is higher-consequence than an action, so it requires `source = 'human'` on the
conversation's kind, which is what `plaudctl kinds --set <id> product` records. `--force`
rewrites generated briefs; it does not manufacture consent.

This does not fix the cause. A single file holding a personal argument and a business
discussion is B15, and no classifier reading a summary of it can be right — which is the
third independent finding this session pointing at the same backlog item.

**A hand-edited brief is never overwritten, `--force` included.** A brief is a working
document; correcting it and handing it on is the whole point. The generated marker lives
*inside the file* rather than in the database, because the file is the artifact — it gets
copied, mailed and pasted into an agent's context, and a provenance claim stored
elsewhere stops travelling with the thing it describes.

### D34 — A local address is not locality: Ollama's cloud runs through 127.0.0.1
`is_local()` decided whether transcripts leave this machine by looking at the host, and
that was correct until Ollama shipped hosted models. They are pulled like any other
model, named with a `-cloud` suffix (`gpt-oss:120b-cloud`), and **addressed at
`127.0.0.1:11434`** — the local daemon forwards the prompt to Ollama's servers.

So the address check would have reported *"nothing leaves this machine"* while a therapy
session was in flight, and `remote_allowed()` would have waved every tier through
because it believed the provider was local. That is exactly the failure D27 exists to
prevent, arriving through the one door D27 did not watch. Nobody had to misconfigure
anything: pulling a cloud model and pointing `ollama_model` at it is the documented way
to use them.

A cloud-suffixed model name now makes the provider remote regardless of host, and the
tier scope applies unchanged. Detection is deliberately broad and anchored to a
separator — `cloudburst:7b` is not caught, `qwen3:cloud` is — because a model wrongly
called remote costs a line of config and a model wrongly called local costs a
conversation you cannot take back.

**Embeddings are refused outright rather than tier-scoped.** Every other model call
handles one recording and can be gated by its tier. Indexing sends *every sentence in
the archive* in one sweep, with no per-recording decision to hang a gate on, so there is
no scope that makes a hosted embedder proportionate. `search.available()` refuses before
the first request and `embed()` refuses again at the point text would go on the wire.

### D35 — The cloud is bought per step, not per installation
A large model is the obvious answer to B8's remaining question, and `cloud_model` plus
`--cloud` makes one available on demand. What matters is *which* steps may spend it.

Summarising, extracting and scoring tone are hundreds of calls over whole transcripts.
Selection is **one call per recording** and is pure judgement — and it sends only the
candidate list and the summary, never the transcript. Writing a brief is one call per
confirmed conversation and its output is read by an agent. Those two are where a large
model changes an answer rather than a rendering, and they are also where the least text
travels per unit of value. So `--cloud` applies to those and nothing else: switching the
whole provider to buy quality on the few would send the many.

No safety machinery is special-cased for it. `with_cloud()` returns an ordinary remote
config, so every call consults `cloud_tier_scope` per recording exactly as a hosted
endpoint would, and a tier outside the scope **raises rather than falling back** to the
local model — a silent downgrade would leave two models' work on one board with nothing
recording which wrote what (the same argument as D14's corollary about sentiment).

### D36 — The recording is the master; conversations are a view over it
A pin left running all day produces one file holding several unrelated conversations.
Title, summary, tone, kind and action budget are all per-conversation and all wrong for
such a file, and this turned up three separate times while building D30-D33: it broke
the budget on the worst recordings, it made the classifier call them `other`, and it
produced a brief for a conversation that was mostly a personal argument.

The obvious fix — split the file into several recordings — is refused. D24 says the
archive is browsable through links and is never renamed, and splitting is a rename of
the most fundamental kind: it would rewrite `recording_id`s already cited in actions and
briefs, invalidate the `stack/` mirror, and re-key the search index. More importantly it
would edit the one thing in this archive that cannot be rebuilt.

**So no audio is ever cut, copied or moved.** A segment is a time range plus an
identity. The file on disk stays exactly as it came off the device, the transcript stays
one document, and `segment.transcript_for()` reads a view of it rather than a copy.
Delete every row in `segments` and the archive is byte-for-byte what it was. Two views,
one master: the **library** is 94 recordings and always will be; the **working view** is
126 conversations, and it is where titles, kinds, budgets, actions and briefs belong.

**An unsegmented recording is one segment spanning the whole file**, computed and never
stored. That is not a special case bolted on at every call site — it is what an
unsegmented recording has always meant, made explicit, so all 94 existing recordings had
a valid segment list the day this shipped with nothing backfilled. Storing 94 rows that
each say "the whole thing" would turn a derived convenience into state that can drift
from the recording it describes.

**Proposed boundaries are derived; confirmed ones are precious.** Every decision
downstream — a tier, an accepted action, a brief — hangs off a boundary, so a re-run
that silently moved one would orphan all of them with nothing in the data saying when.
Same rule as a confirmed speaker name (D18) and a hand-set conversation kind (D30).

**Silence is the signal. A change of cast was built, measured, and demoted.** Both
looked equally promising. Scoring turnover in the set of diarization labels produced
**90 conversations from one 4.5-hour recording and 19 from a single 55-minute
interview** — because the labels are noisy and unnamed (218 unnamed voices across 73
recordings here), so the label set in any sliding window churns whether or not anybody
left the room. It was measuring the diarizer. Silence at three minutes gives 5
conversations for a 4.5-hour file and 4 for a 5-hour one, and leaves a 47-minute prayer
session whole — correct, since that recording's problem was always its kind. Turnover is
still reported as evidence beside a silence that stands on its own; it can no longer
open a boundary by itself.

**Duration does not make a conversation; speech does.** Two long silences in a row leave
a sliver between them, and one such span on a real recording ran seven minutes and held
368 characters of speech. A span with under a minute of actual talking is folded
backwards into the conversation before it — folded rather than dropped, because a span
belonging to no segment is archive nobody can reach through the working view.

**What silence cannot catch** is one conversation ending and the next starting with no
pause. That is a change of subject, visible only in what was said, and for a three-hour
recording it needs a model that can hold the whole transcript at once — which is what
D35's `cloud_model` and the `num_ctx` fix exist for. Not built yet.

### D37 — The pipeline runs per conversation, and the review that followed
D36 built the working view and nothing consumed it: `kinds`, `extract` and `brief` still
asked one question of a file holding four conversations. A conversation is
`(recording_id, segment_idx)`, `store.conversations()` is the view, and all three now
iterate it.

**The migration was free because segment 0 already meant the right thing.**
`conversation_kinds` is re-keyed on `(recording_id, segment_idx)` — SQLite cannot alter
a primary key, so the table is rebuilt — and `actions` gains `segment_idx` defaulting to
0. Every pre-existing row described the whole recording, which is exactly what segment 0
of an unsegmented recording is. Verified on the live archive: 97 recordings, 982 actions,
74 kinds, 1564 events, all preserved, no temporary table left behind.

Three defects surfaced while wiring it, each caught by writing the test rather than by
reading the code:

*The recording clock.* `extracted_at` is a column on the recording. Stamping it when the
first of four conversations finished marked the whole file done and skipped the other
three on the next run. It is now set only once every conversation in that recording has
been handled in the pass.

*Stale kinds.* A kind the model inferred described the conversation as it was bounded
then. Re-bounding the file makes that claim about a conversation that no longer exists —
and leaving it is worse than having none, because it sits on segment 0 looking current
while `needing_kind` never queues that segment again. Model-set kinds are dropped when a
segmentation changes; a person's is kept (D30).

*Orphaned actions.* `clear_segments` left actions pointing at segments that no longer
existed: in the database, on no board, reported by nothing. They come home to segment 0
now, and when a segmentation is *created* they are re-attributed by `at_ms` — which
recovers their placement rather than losing it. On the live archive that spread 982
actions as 770 / 184 / 18 / 10 across conversations with zero orphans.

**Measured end to end.** One recording now resolves into an interview (budget 1), a
personal conversation (budget 2) and a podcast playing (budget 0, never extracted).
Before, all three were one kind with one budget.

#### What the end-to-end review found

*A context window could cross a conversation boundary.* Chunks are cut at 1200
characters with no idea where a conversation ends, so on a segmented file the neighbours
of a hit near a boundary belong to a different discussion — **56 such windows** on this
archive. B4 would have stitched them into continuous prose for a model to answer from,
which is a more confident kind of wrong than no context at all. `chunk_window` clamps to
the conversation containing the hit; an unsegmented recording windows freely as before,
and a chunk with no timestamp keeps its neighbours rather than silently losing them.

*The judge was still pooling per file.* It sampled every action of a recording and scored
selection against one kind and one budget — measuring a board no version of this product
shows anybody. Sampling and measurement are both per conversation now. This mattered more
than the others: it is the instrument.

*The tier scope bounded reading but not writing.* An audit of all eleven MCP tools found
`propose_action` was the one archive access with no tier check. Nothing leaked — it is a
write — but a client scoped to `stack` could attach a proposal to a `local` recording's
board. "Proposals are harmless" is not the argument: the board is the owner's, and an
entry referencing a conversation the client was never shown is a scope violation
whichever way the data moved.

*A script could not parse on the Python this project claims to support.*
`scripts/sync-docs.py` — which the post-commit hook runs on every commit — used a
backslash inside an f-string expression, a syntax error before 3.12, while
`requires-python` says 3.11. It worked because the machine it ran on has 3.13, and
nothing caught it because CI linted `plaudvault` and `tests` and not `scripts`. Both
fixed; the same file also had a lambda closing over a loop variable, which would have
substituted the last block's body into every block.

*The diagrams were hand-exported and could not be checked.* `scripts/render-diagrams.py`
renders `.excalidraw` to PNG with Pillow, so the picture is a build artifact of the
source rather than a memory of it. Deliberately small — it draws the element types these
diagrams use and nothing else, because the alternative was a headless Chromium to
reproduce a hand-drawn stroke style that carries no information.

**Measured cost, accepted for now.** `conversations()` is N+1: 260 queries for 124
conversations, 28 ms. That is a tenth of a single embedding call and it is the same
brute-force trade as D7 — revisit at roughly ten times this corpus, where it becomes a
few hundred milliseconds on a page load.

**Still per file, and honestly so.** Summaries, titles, tone and the search index are
one-per-recording. On the 20 multi-conversation recordings that means 18 summaries
averaging several conversations, 18 titles naming one of them, and hits that cite the
file rather than the conversation. None of that is wrong today; all of it is coarse, and
it is the next piece of work rather than a defect in this one.

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
_Generated 2026-09-19 from git and the live archive._

### Codebase

| | |
|---|---|
| Python modules | 34 |
| Lines of Python | 10,763 |
| Commits | 31 |
| CLI verbs | 30 — `login`, `logout`, `status`, `fresh`, `sync`, `verify`, `brief`, `judge`, `kinds`, `index`, `search`, `story`, `title`, `diarize`, `speakers`, `dispatch`, `mcp`, `eval`, `tier`, `browse`, `web`, `init`, `service`, `run`, `prune`, `transcribe`, `summarize`, `sentiment`, `notes`, `extract` |

Largest modules: `cli.py` (1194), `store.py` (1024), `web.py` (935), `story.py` (803), `mcp_server.py` (550), `diarize.py` (536).

### Live archive

| | |
|---|---|
| Recordings | 94 |
| Transcribed | 94 |
| Tone scored | 61 |
| Indexed chunks | 2,559 |
| Triaged | 85 |
| Open commitments | 410 |
| Action events | 1,564 |
| Audio captured | 76.7 hours |
| Tiers | exclude 3 · stack 82 |

<!-- END:STATUS -->

---

## 6. Shipped

<!-- BEGIN:SHIPPED (generated — do not edit by hand) -->
Newest first. Each commit message carries the full reasoning; this is the index.
Find one with `git log --grep="<subject>"`.

| Date | What landed |
|---|---|
| 2026-09-19 | Record the decisions, and what the measurements cost to learn |
| 2026-09-19 | A local address is not locality: Ollama's cloud runs through 127.0.0.1 |
| 2026-09-19 | Give a conversation a kind, a budget, and a way to spend it |
| 2026-09-19 | Give a search hit its neighbours, without loosening the citation |
| 2026-09-19 | Run the tests in CI, and find a dead tone-scorer doing it |
| 2026-09-04 | Redraw the diagrams against what the pipeline actually is |
| 2026-09-03 | Make a stated constraint a filter, and give the cloud a tier scope |
| 2026-09-02 | Tell me when the console is running code older than the files on disk |
| 2026-09-02 | Play the voice that only ever interjects, and play it from the inbox |
| 2026-09-02 | Hear a voice before you name it, and name it without opening a dialog |
| 2026-09-02 | Copy a transcript in one click, and give the archive names a person can read |
| 2026-09-02 | Count summarized against what is eligible, and show the title you already have |
| 2026-08-31 | Refuse generated queries that are the prompt's own examples |
| 2026-08-31 | Report one unnamed-voice count, not two |
| 2026-08-31 | Measure retrieval instead of asserting it, and sketch the pipeline under Dagster |
| 2026-08-30 | Diarization survives contact with a real archive |
| 2026-08-30 | Take the HuggingFace token from stdin when there is no terminal |
| 2026-08-30 | Name the recordings, name the voices, and let an agent do the work |
| 2026-08-24 | Replace a real consultation quote in the journeys diagram with a synthetic one |
| 2026-08-24 | Bulk edits, and the corpus drawn as themes over time |
| 2026-08-24 | Draw a recording along its own duration, not as a grid of cards |
| 2026-08-24 | Dismissing noise takes it out of the console and out of the pipeline |
| 2026-08-24 | Refresh bible status after the archive grew |
| 2026-08-23 | Drop the SHA column: a table cannot contain its own commit hash |
| 2026-08-23 | Document the whole thing: three diagrams, a product bible, and a way to keep it current |
| 2026-08-22 | Step between recordings without going back to the list |
| 2026-08-22 | Semantic search over transcripts, with timestamped hits |
| 2026-08-22 | Extract commitments only; suggestions become opt-in |
| 2026-08-22 | Refuse extracted actions whose quote isn't in the transcript |
| 2026-08-21 | Keep excluded recordings off the trend, and verify launchd actually loaded |
| 2026-08-21 | Own your Plaud recordings end to end |
<!-- END:SHIPPED -->

---

## 7. Backlog

Ordered within each tier by expected value, not effort. Nothing here is committed.

### Next — clear value, design settled

| # | Item | Why |
|---|---|---|
| B14 | **A verified golden set** | B3 built the instrument; every one of its 48 queries is still `verified: false`, because each was written *from* the recording it is scored against. That measures a strictly easier task than the real one, so the harness currently has no number anybody is allowed to quote. Queries written from memory, before looking, are the fix. Blocks B4, B5 and D-open-1, all of which are comparisons against a baseline. |
| B16 | **Due-date resolution during extraction** | "By Friday" has to become a date while the transcript is in front of the model, or every calendar invite needs a human to retype it. 4 of 681 actions carry one. |
| B15 | ~~Split a recording at conversation boundaries~~ — **shipped as D36**, as a view rather than a split. What remains is the second signal: a boundary with no silence, which needs a model reading the whole transcript. | A pin left running produces one file holding several unrelated conversations. Title, summary, tone and tier are all per-recording and all wrong for such a file, and its chunk count lets it dominate retrieval. Diarization already knows where the voices change; a gap plus a speaker-set change is most of a boundary detector. |

### Later — valuable, design not settled

| # | Item | Open question |
|---|---|---|
| B6 | **Topic trends over time** | Aggregation, not retrieval — clustering or per-period map-reduce. "How has my thinking on X changed" is a different build from RAG. |
| B11 | **Re-extract after diarization** | Named transcripts should improve `owner` on extracted commitments, which is half of B8. Nothing re-runs extraction when speakers change, so the gain is currently only realised on recordings diarized before their first extract. |
| B12 | **Contact-reference resolution** | `speakers.external_ref` carries an opaque id and nothing resolves it. Making "map this conversation to my CRM" real needs a resolver per system, and the question is whether plaudvault should hold one at all or hand the string to the agent. |
| B13 | **Reaching the archive from off-machine** | The MCP server is stdio and the console is loopback-only, so an agent on a remote VM cannot reach either. Needs a real identity proxy before it needs code — see Rejected. |
| B8 | **Better commitment precision** | Built: D30 (a budget per kind), D31 (selection spends it), D32 (the instrument to measure it). What is missing is not code — it is **verdicts**. `plaudctl judge` has an empty set, so precision@budget has no value anybody may quote, and D31's claim rests on a proxy (meta-talk, 21.2% → 0.0%) rather than on agreement with the owner. One labelling sitting turns every future change to this from an argument into a number. |
| B9 | **Backup/restore command** | The precious/derived split in the data-model diagram is the spec. Nobody has written the command. |
| B10 | **Notification when a scheduled run fails** | A 07:00 sync with the drive unmounted is a silent no-op. Freshness surfaces it only if you look. |

### Rejected — recorded so they are not re-proposed

| Item | Why not |
|---|---|
| Vector database (Chroma, sqlite-vec, …) | D7. Revisit at ~10⁵ chunks. |
| `qwen3.8` / larger local model | D14. Physically cannot load on 24 GB. |
| Filtering suggestions out of the LLM response | D10. Asking and discarding still spends the model's attention inventing them. |
| Pure verbatim quote matching | D9. Measured: would discard 23 sound actions to catch 2 bad ones. |
| Exposing the console beyond loopback | No auth by design. Would need a real identity proxy first. An agent on a remote VM reaching this archive is B13, and it is a networking-and-identity problem, not a plaudvault feature. |
| Feeding automatic speaker matches back into voiceprints | D18. One bad match compounds into a drifting identity with nothing in the data saying when it went wrong. |
| A flag to dispatch a `proposed` action | D20. The acceptance step *is* the human reading the quote. |
| A lexical rubric for ranking actions | D31. Built and measured: 1 of 7 kept actions inside budget, mean position 0.382 against 0.449 for extraction order. "Define the compensation band" and "Identify the target audience" are the same sentence and got opposite verdicts — what separates them is not in the sentence. |
| A per-item second-pass judge | D31. Superseded rather than rejected: one *comparative* call per recording is cheaper (≈7% on top of extraction), and a small model choosing among candidates beats the same model scoring one in isolation. |
| A bigger action budget for product conversations | D33. Sixty checkboxes is a specification shredded into sixty pieces. The artifact wanted is a brief. |
| Filtering search hits after ranking | D26. Returns three hits when eight were asked for, and cannot distinguish "nothing matched" from "the matches were filtered out". |
| A global switch for sending transcripts to a cloud model | D27. This archive is not one kind of conversation, and one switch for all of it is how a therapy session reaches a vendor. |

---

## 8. Roadmap

**Now — a number worth quoting.** B3 shipped, and the first full-corpus run put oblique
recall@1 at 0.55: ask the archive for the gist of something in words it did not use, and
it finds the right recording about half the time. That is the honest state of retrieval,
and an MCP client is writing confident prose over it.

But the score is provisional in a way that matters more than its value. All 48 queries
were generated from the recordings they are scored against, so the harness is measuring
an easier task than the real one and every query is flagged `verified: false`. B14 first —
without it, B5 is an improvement measured against a baseline that does not hold.

B4 shipped ahead of that order on purpose: it does not change *what* ranks, only how much
text a hit arrives with, so it needs no baseline to justify and it was the change that
put a test under the MCP read path (D28, D29).

**Next — make the identity layer earn its cost.** Diarization is built but starts empty,
and its value is entirely in what gets named. D25 removed the two things that made naming
expensive — you can hear a voice before naming it, and naming is inline rather than a
dialog — so the queue of 127 unnamed voices is now a sitting worth doing rather than an
afternoon. Then B11, because named transcripts are the cheapest available attack on B8.

**Then — extraction quality, now a measurement problem.** D30, D31 and D33 built the
whole chain: a budget per conversation kind, a comparative selection pass that spends it,
overflow so nothing is lost, and a brief for the one kind where a checklist was the wrong
artifact. Extraction went from a median of 12 actions per conversation to 1-3, and
meta-talk on the board from 21.2% to 0.0%.

**What is missing is verdicts, not code.** D32 built the instrument and its set is empty.
Until a labelling sitting happens, the honest claim is "meta-talk is gone and the board
is short", not "the right three are on it" — and every future change to selection is an
argument rather than a comparison. That sitting is the highest-value hour available
anywhere in this project.

B15 moved up on the way: the worst-yielding recordings are several conversations in one
file, and no per-recording budget can fix that.

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
(7 of 57 at time of writing), by design.

**Since B2 shipped, "the stack should call plaudvault" is literal.** `plaudctl mcp`
serves search, transcripts, speakers and the action queue over stdio to any MCP client.
It returns cited passages and never paraphrases — the client's model does the synthesis,
which is why B1 (ask-with-citations *inside* plaudvault) was dropped rather than built:
the retrieval belongs here, the synthesis belongs in whatever asked, and a paraphrase
with no timestamp is exactly the thing you cannot check.

The MCP client's scope is a *different* question from the stack's corpus, and D21
settles it: the default is what the console sees, because the server is launched by your
own agent on your own machine. `--tiers stack` narrows it per client.

---

## 10. Known limits

Stated plainly because each one is a way this product can mislead you.

- **Speaker identity is a similarity judgement, not recognition.** A voiceprint match is
  cosine similarity above a threshold. Two similar voices, a bad line, or a recording where
  somebody is ill will all move it. The console draws machine matches as guesses for this
  reason; treat an unconfirmed name as a prompt to check, not as a fact.
- **Diarization does not know who anybody is.** It knows how many voices there are and
  when each spoke. Everything else is the identity layer, which starts empty.
- **A title is a summary of a summary.** It inherits every weakness of the summary it was
  written from, compressed further. It is a way to find a recording, not a description of
  one.
- **A long recording is not one conversation, and everything downstream assumes it is.**
  The 2.9-hour file titled "Metric Health SOC2 Compliance Audit" also contains a job
  interview and two unrelated introductions. One title, one summary and one tone score are
  stretched over all of it, and its 139 chunks — seven times the median — put it at or near
  the top of almost every search regardless of the question. The per-recording cap of 3
  keeps it from owning a whole page but does not stop it owning the first row. Splitting a
  recording at conversation boundaries is the fix and is not built (B15).
- **An agent's report is unverified.** plaudvault records what the agent said it did. It
  has no way to check, which is why the action stays open until you close it.
- **Tone scores are estimates over ASR.** A transcript has no tone of voice, so sarcasm,
  warmth, and a calm discussion of something painful all read the same on the page.
- **Confidence is currently uninformative** (D5). Treat valence as the useful number.
- **Extraction still surfaces rhetoric as commitment** (B8). Treat the board as prompts
  to check recordings, not a task list.
- **Extraction is non-deterministic.** Two runs over the same transcript returned
  different commitments. Re-running `--force` gives a different board.
- **Oblique retrieval is roughly a coin flip.** Measured 2026-08-31 over 48 generated
  queries: naming a person, company or number finds the right recording first 79% of the
  time; describing the same thing in words the recording did not use, 55%. The second
  number is the one that matters, because it is the case semantic search exists for. Both
  are provisional until B14 — the queries were written from the recordings they score.
- **Search scores are cosine similarity, not confidence.** Unrelated English sits around
  0.3–0.5; a top hit at 0.55 may still be the best the archive has. Quality falls off
  after the first few hits.
- **Local ASR differs from Plaud's.** `whisper-large-v3-turbo` caught a 90-second stretch
  Plaud's own transcript dropped entirely, but garbles some crosstalk. Theirs is kept in
  `meta/<id>.json` so you have both.
- **Plaud transcodes asynchronously.** A recording synced minutes after upload may arrive
  as the raw device blob. Detected, kept, retried next run.
- **The console can serve a newer page than its own code.** `index.html` is read from
  disk on every request; the Python is imported once, at start. A console left running
  across an edit therefore serves the new page against the old endpoints, and the symptom
  is a feature that 404s — which reads as a broken feature, not a stale process. This cost
  a real debugging session. `/api/status` now reports `stale_code` and the console says so
  in a banner; `plaudctl service restart` is the fix.
- **Everything depends on one external volume.** Archive *and* models live on it. It
  unmounted once during development: the failure is graceful and self-recovering, but a
  scheduled run with the drive detached is a silent no-op (B10).
- **The suite covers behaviour, not coverage.** 137 tests run in under a second with no
  network, no Ollama and no archive — every one builds its own SQLite in a tmpdir. They
  are aimed at the properties that must not regress silently: tier scope, quote
  grounding, filters applied before ranking, and the stitching above. `web.py`, `cli.py`
  and `story.py` remain untested, and a change there is checked by running it.
- **Only the Apple Silicon path is battle-tested.** faster-whisper, OpenAI-API and
  systemd paths are implemented and unrun on their target platforms.

---

## 11. Open questions

| # | Question | Status |
|---|---|---|
| D-open-1 | Do `search_query:`/`search_document:` prefixes actually help retrieval here? | A 4-query eval was inconclusive — better mean rank (13 vs 15), worse top-3 (2/4 vs 3/4). Kept because they are the model's documented usage and Ollama's template (`{{ .Prompt }}`) confirms it does not add them itself. B3 built the instrument to settle this; it needs B14 before an A/B means anything. |
| D-open-4 | Is 0.55 oblique recall@1 a retrieval problem or an embedding-model problem? | `nomic-embed-text` is the only embedder ever tried. Chunking, prefixes and the model are three knobs and the harness cannot yet tell them apart. |
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
