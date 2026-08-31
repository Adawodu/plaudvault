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
plaudctl run       # sync → transcribe → diarize → summarize → title → tone → notes → extract
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
  reading, who spoke, and its actions. Where you set the tier, rename it, name a voice,
  and mark for cloud deletion.
- **Actions** — accept a proposal (stating what it should achieve), work it, complete
  it with an outcome score. Accepted actions can be handed to an agent.
- **Agents** — what you assigned to an agent, and what it reported back.
- **Speakers** — the people the archive can recognise by voice.
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
- **exclude** — noise. Dismissed: gone from every console surface *and* skipped by the
  rest of the pipeline, so it stops costing you model time. The audio and its
  verification facts stay on the drive untouched.

Re-tier something out of *stack* and the copy is deleted on the next sync. The
decision is enforced by what exists on disk, not by a flag downstream tools have to
remember to respect.

That guarantee covers what plaudvault owns. It does **not** survive a tool that copies
`stack/` into its own index: deleting the file here cannot reach a row and an embedding
that already live somewhere else. A downstream index must either re-scan `stack/` and
prune what has vanished, or — better — not hold a copy at all and query this archive
instead. See the Cognitive Stack boundary in `docs/PRODUCT-BIBLE.md` §9.

### Bulk edits

Tick the checkbox on any card in the Inbox, Library or Actions and a bar appears with
what you can do to the whole selection: triage or dismiss recordings, accept, start or
reject proposals. **select all** takes everything currently listed, which respects the
filter you are looking through.

Every transition still goes through the same code path as a single edit, so each one is
journalled to `action_events` individually — bulk is a convenience, never a shortcut past
the audit trail. The selection resets when you change tabs, because acting on things you
can no longer see is how bulk edits go wrong.

### Dismissing noise

Not every recording deserves your attention. A thirty-second misfire, a pocket
recording, the demo files that shipped with the device — press **dismiss** on any card
and it leaves the Inbox, the Library, Search, Trends and the `stack/` corpus in one
click, with no dialog.

Three properties make that safe to do freely:

- **Nothing is deleted.** The audio, its sha256 and its size/container facts are
  untouched, so the recording stays verifiable and prunable later.
- **It never comes back.** Triage lives in its own table that `sync` never writes to,
  so re-syncing refreshes the metadata and leaves your decision alone.
- **It stops costing you.** Summarize, tone, extract and index all skip it, and
  freshness stops counting it as work owed — otherwise the pill would sit amber
  forever over work nobody wants done.

Dismissed recordings are still there: tick **show dismissed** in the Library to review
or **restore** them. The Library always prints how many are hidden, so "quiet by
default" never becomes "silently missing".

### If you delete from Plaud directly

Perfectly fine — your drive is the copy of record. `sync` only ever *adds*: it never
deletes a local row or a local file because something vanished from Plaud's cloud. You
can empty the Plaud account entirely and the archive is unaffected.

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

## Titles

Plaud names a file after the clock — `2026-07-14 09:12`. Accurate, and useless: thirty
rows of timestamps tell you nothing about which one was the call with the lawyer. After a
recording is summarized, the model reads that summary and proposes a name.

```bash
plaudctl title              # name anything unnamed
plaudctl title --force      # re-title the model's own work, never yours
```

It is a proposal like everything else here. Press **rename** in the console to write your
own, and a title you wrote is never overwritten by a re-run — clear the box instead to
hand it back to the model. The device's filename is always shown alongside, so a title
you disagree with never hides what the file actually is.

When the model cannot find a subject it says so and the recording stays unnamed, rather
than being filed as "Business Discussion". On the live corpus that was 49 of 50 named and
1 correctly declined — thirty rows of "General Conversation" would be no better than
thirty timestamps.

## Who is speaking

Transcription alone produces one undifferentiated monologue, which costs more than
readability: an extracted commitment has an `owner` the model can only guess at, and a
tone score cannot tell your frustration from someone else's.

```bash
plaudctl speakers status                      # what's set up, what isn't
plaudctl speakers login                       # store a HuggingFace token
plaudctl diarize                              # split recordings by voice
plaudctl speakers unknown                     # voices nobody has named
plaudctl speakers name <rec> SPEAKER_00 --name Bayo --me
plaudctl speakers rematch                     # find that voice everywhere else
plaudctl speakers link --speaker 1 --ref clarify:rec_123
```

Diarization gives you anonymous labels — `SPEAKER_00`, `SPEAKER_01`. Those are
per-recording and useless across an archive, because `SPEAKER_00` is a different person in
every file. The value is in the identity you lay over them: **name a voice once and its
voiceprint is kept, so the next recording matches by voice rather than asking again.**
Every recording here is yours, so the cheapest first move is to confirm yourself once.

Two rules keep that honest:

- **Only your confirmations build a voiceprint.** An automatic match is drawn as a guess
  and never feeds back into the mean. Otherwise one bad match compounds until the identity
  is whoever the machine has been mistaking for you, with nothing in the data saying when
  it went wrong.
- **Re-running diarization never un-names anybody.** Your decision lives in its own
  column, exactly as triage survives a re-sync. Correcting an attribution rebuilds that
  person's voiceprint without it, and renaming somebody rewrites every transcript they
  appear in.

A voice must speak for 30 seconds (`speaker_min_seconds`) before you are asked to name
it. A pin worn through a shopping trip hears the shopkeeper and a child three aisles away
— on a real recording that was eight voices, three under half a minute. They stay on the
recording and can still be named there; only the work list is filtered, and the hidden
count is always shown. `plaudctl speakers unknown --all` includes them.

Each person carries an optional **contact reference** — an opaque id pointing at whatever
system holds the rest of that relationship. plaudvault never has to know whose CRM it is;
it carries the string, and an agent asking `list_speakers` can join a voice to a record.

Diarization is an extra, because pyannote pulls ~2 GB of torch and its models are gated:

```bash
pip install 'plaudvault[speakers]'
```

Then accept the licence, while signed in, at
[pyannote/speaker-diarization-community-1](https://hf.co/pyannote/speaker-diarization-community-1)
and [pyannote/segmentation-3.0](https://hf.co/pyannote/segmentation-3.0), and run
`plaudctl speakers login`. Everything else in plaudvault works without any of this, and
freshness will not nag you about undiarized recordings on a machine that cannot diarize.

## Asking the archive from an agent

```bash
pip install 'plaudvault[mcp]'
plaudctl mcp                    # stdio, for an MCP client to launch
plaudctl mcp --tiers stack      # hand this client a narrower view
```

Registers like any stdio MCP server. For Claude Code:

```bash
claude mcp add --scope user plaudvault -- /path/to/.venv/bin/python -m plaudvault.cli mcp
```

Ten tools, in two halves. **Read:** `search_recordings`, `get_recording`,
`get_transcript`, `list_recordings`, `list_speakers`, `list_actions`. **Act:**
`my_tasks`, `claim_task`, `report_task`, `propose_action`.

Search returns **cited passages** — recording, timestamp, tier, and the words themselves.
The client's model does the synthesis; this server does the retrieval and never
paraphrases, because a paraphrase with no timestamp is exactly the thing you cannot check.

Tier is enforced here and nowhere else. `mcp_tier_scope` decides what a client may read
and defaults to what the console shows; `exclude` is unreachable through every path
regardless, and audio is never served. `--tiers stack` narrows one client without changing
the others.

**It is stdio, on this machine.** An agent running on a remote VM cannot reach it, and the
console is loopback-only for the same reason. That is a networking-and-identity problem,
not a missing feature — put a real identity proxy in front of it before you reach for a
tunnel.

## Handing work to an agent

```bash
plaudctl dispatch agents                                   # who is configured
plaudctl dispatch assign 42 --agent openclaw --instructions "propose three slots next week"
plaudctl dispatch list --status done
plaudctl dispatch cancel 3
```

An accepted action can be assigned to an agent. **plaudvault never runs the work.** It
writes the request and waits. The agent calls `my_tasks`, claims a job so no two agents do
the same thing, does the work in its own world, and reports back to the **Agents** tab.

Three constraints, none with an override:

1. **Only an accepted action can be handed over.** `proposed` is the extractor's guess,
   and the extractor is measured to over-propose. Acceptance is the step where a human
   read the quote.
2. **Dispatch is a request, never an execution.** Whatever the agent can do, it could
   already do; this only tells it what you want.
3. **A finished job is a report, not a completion.** The result lands on the dispatch row
   and the action stays open. An agent that believes it booked a meeting and did not must
   not be able to tick the box itself.

The quote from the recording travels with the job, because an agent told to "set up the
meeting" with no source cannot tell a real commitment from a garbled one.

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

## The shape of a conversation

A recording is not a grid of cards. It has a beginning and an end, it moves, and things
get said at particular moments. So it is drawn along its own duration: tone fills the
band, and every commitment is pinned at the minute it was spoken. Where the band shifts
is where the conversation turned; a cluster of pins is where the work got decided. You
can read the shape before you read a word.

Open any recording and press **draw it**, or from a terminal:

```bash
plaudctl story                              # the most recently scored recording
plaudctl story <id> --format excalidraw     # editable scene instead of SVG
```

Two renderers over one layout. **SVG** goes straight into the console — live, themed for
light and dark, no dependency. **`.excalidraw`** is the same picture as an editable
scene, so you can open it, drag things and write on it. A picture you can annotate is
yours in a way a generated report is not.

Only the commitments that earned it get a label — anything you accepted or completed
first, then the earliest. The rest stay as ticks on the band with an honest count, because
a label on every pin is chaos and goes unread. On a busy conversation the layout grows
downward rather than pushing labels up through the title.

Two honest limits are printed on the picture itself: tone is an estimate over a
transcript, and segment widths are **proportional, not measured** — sentiment chunks are
equal slices of text, not equal slices of time.

## What you keep coming back to

The **Trends** tab also draws the whole corpus as one picture: themes over time, with the
tone underneath. Or `plaudctl story --arc`.

Themes come from **clustering the embeddings**, not from the summariser's tags. That is
not a preference, it is what the data forced: 155 distinct tags across 31 summaries and
only three recurring even three times, because the model invents fresh vocabulary every
run. Tags cannot thread a story. Vectors can — two conversations about the same thing
land near each other whatever words they happened to use.

Each cluster is named by the words that **distinguish** it, not the words it uses most.
Counting frequent words named every cluster *"it's · that's · don't"*, so a word is
scored against how many clusters use it and one used by most of them is dropped outright.

Two things the picture is deliberately honest about:

- **The axis breaks.** Strict time-proportional spacing was tried and rejected: a
  three-month gap swallowed 934 px of a 1180 px axis and squeezed the weeks that matter
  into 250 px. The gap is now drawn as an explicit break, labelled with how many weeks
  were recorded nothing — so the discontinuity is visible rather than smoothed away.
- **Expect one or two themes to be junk.** Clustering finds structure whether or not the
  structure means anything, and the caption on the picture says so.

Clusters surface real vocabulary from real conversations, including names and raw
language. Dismissed recordings are excluded, but nothing else is filtered.

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

- **A voiceprint match is similarity, not recognition.** Two similar voices, a bad line,
  or a day when somebody is ill will all move it. Unconfirmed names are shown as guesses;
  treat one as a prompt to check.
- **Diarization does not know who anybody is.** It knows how many voices there are and
  when each spoke. The identity layer starts empty and is worth exactly what you put into it.
- **A title is a summary of a summary**, so it inherits every weakness of the summary and
  compresses it further. It is a way to find a recording, not a description of one.
- **An agent's report is unverified.** plaudvault records what the agent said it did and
  has no way to check — which is why the action stays open until you close it.
- **Extraction does not re-run when speakers are named.** Named transcripts should improve
  the `owner` on commitments, but nothing currently re-extracts, so that gain is not
  realised on recordings extracted before they were diarized.

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
