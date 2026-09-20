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
plaudctl run       # sync → transcribe → diarize → summarize → title → tone
                   #   → notes → extract → index → tier → browse
plaudctl fresh     # is the vault up to date? (--cloud also asks Plaud)
plaudctl status    # what's archived, what's pending, what's healthy
plaudctl verify    # re-hash the archive, catch bitrot or missing files
plaudctl browse    # PLAUD/by-name/ — the archive under readable names
plaudctl web       # the console
```

### Running it automatically

```bash
plaudctl service install                 # console always on, sync 4x/day
plaudctl service install --hours 8,13,18 # or pick your own
plaudctl service status
plaudctl service restart                 # after a code change — see below
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

## One file, several conversations

Leave the pin running all day and you get one recording holding a standup, a school run
and a client call. Everything downstream is per-conversation and all of it is wrong for
that file: one title for three topics, one summary that averages them, one action budget
spread across material with nothing in common.

```bash
plaudctl segments                      # find the boundaries
plaudctl segments --show <id>          # see them
plaudctl segments --confirm <id>       # make them permanent
plaudctl segments --clear <id>         # back to one conversation
```

**Nothing is ever cut.** A segment is a time range plus an identity. The audio file stays
exactly as it came off the device, the transcript stays one document, and a segment reads
a *view* of it rather than a copy. Delete every segment and the archive is byte-for-byte
what it was.

Two views over one master:

| | |
|---|---|
| **Library** | every recording as it arrived — the source of truth, never reorganised |
| **Working view** | the conversations inside them, where titles, kinds, budgets, actions and briefs belong |

On the reference archive: 97 recordings in the library, 20 of which hold more than one
conversation — 124 conversations in the working view.

**The whole pipeline runs per conversation.** `kinds`, `extract` and `brief` each work on
one conversation at a time, so a file holding a standup and a school run gets two kinds,
two budgets, two boards and, where it earns one, two briefs. One real recording resolves
into an interview (budget 1), a personal conversation (budget 2) and a podcast playing
(budget 0 — never extracted at all). Before, all three shared one kind and one budget.

Actions remember which conversation they came from, and re-segmenting a recording
**re-attributes them by their timestamp** rather than losing their placement. Clearing a
segmentation brings them home. Nothing is ever stranded in the database on no board.

A recording with no segmentation **is** one segment covering the whole file. That is not
a special case; it is what an unsegmented recording has always meant, so every existing
recording had a valid segment list the day this shipped, with nothing backfilled.

**Boundaries are proposed, then confirmed.** A proposal can be replaced by a later run; a
boundary you confirm is never moved by one. Every decision downstream — a tier, an
accepted action, a brief — hangs off a boundary, and silently moving one orphans all of
them.

**Silence is the signal, and a change of voices is not.** Both were built. Scoring the
turnover in diarization labels produced 90 conversations from one 4.5-hour recording and
19 from a single interview — the labels are noisy and unnamed, so the set of them churns
whether or not anyone left the room. It was measuring the diarizer. A three-minute
silence gives 5 conversations for that same 4.5-hour file, and leaves a 47-minute prayer
session whole. Voice turnover is still shown as evidence next to a silence; it cannot
open a boundary on its own.

Duration alone does not make a conversation either: a span with under a minute of actual
speech is folded into the one before it, because two long silences in a row leave a
sliver of dead air that duration calls a conversation and the audio does not.

Search respects the boundaries too. `--context` stitches a hit to its neighbours, and on
a segmented file the chunk next door can belong to a different discussion — 56 such
windows existed here before this was fixed. A window never crosses a conversation now,
because handing a model two unrelated conversations as continuous speech is a more
confident kind of wrong than giving it no context at all.

**Still one-per-file, honestly:** summaries, titles, tone and the search index. On the 20
multi-conversation recordings that means a summary averaging several conversations and a
title naming one of them. Coarse rather than wrong, and the next piece of work.

What silence cannot catch is one conversation ending and the next starting with no pause
— a change of subject, visible only in what was said. That needs a model reading the
whole transcript, which is what `cloud_model` is for. Not built yet.

## Actions

### What kind of conversation was this?

Before anything is extracted, `plaudctl kinds` classifies each recording from its summary
into a fixed vocabulary, and each kind carries a **budget** — how many actions such a
conversation should be expected to yield.

| kind | budget | |
|---|---|---|
| `working` | 3 | decisions, planning, a standup, a client call |
| `product` | 3 | specifying something to be built, researched or executed |
| `interview` | 1 | much of it is a CV or a job description read aloud |
| `personal` | 2 | family, logistics, health, money — real commitments, small ones |
| `devotional` | 1 | prayer, worship, scripture, a sermon or teaching |
| `media` | 0 | a recording of something playing; nobody in the room is committing |
| `other` | 3 | none of the above |

This exists because the numbers demanded it. Extraction ran identically on everything,
and on a real archive that meant a **median of 12 actions per conversation against a
wanted 2–3, a maximum of 69, and 567 of 982 dropped by hand.** One prayer session
produced 69 action items. Nothing was broken: extraction asks each chunk "what
commitments are here?", a long recording is fifteen chunks, and a leading question gets
answered fifteen times.

A budget of 0 means extraction **never runs** — which is not the same as running and
finding nothing. The console says "no actions expected from a devotional" instead of
showing an empty board that reads as a failure.

```bash
plaudctl kinds                      # classify what isn't classified yet
plaudctl kinds --list               # the breakdown
plaudctl kinds --set <id> product   # correct one by hand; a re-run will never undo it
```

The vocabulary is **fixed, not learned**. A clustering would drift with the corpus and
drag the budgets, the console labels and the MCP contract with it. Six kinds you can hold
in your head is a schema; a clustering is a snapshot. A model that invents a kind lands on
`other` rather than being coerced into `working` — coercion hands a budget to something
nobody classified.

`devotional` was 0 until the archive overruled it: a sermon produced *"commit to breaking
bread with someone in an intentional way at least once a month"*, which its owner is
acting on. A teaching conversation is not a working session, but it is not empty either.

### Choosing which three

Extraction is asked once per chunk and over-produces on purpose — recall first. One
further call per *recording* then spends the budget, with the conversation's summary for
context and every candidate visible at once.

A lexical rubric was built for this first and thrown away. Scored against the only ground
truth the archive held — seven accepted actions against 975 that were not — it placed
**one of seven** inside its budget, and ranked kept items at mean position 0.382 where
extraction order managed 0.449 and random 0.487. The failure is the instructive part:
*"Define the compensation band for the role"* was dropped and *"Identify the target
audience for the app"* was kept, and those are the same sentence. What separates them is
whose commitment it is and whether it was decided or merely aired — **neither of which
is in the sentence**, so no sentence-scorer can find it.

A comparative call can see what a scorer cannot, and it is cheap: extraction already
makes ~15 calls per recording, so precision costs about 7% more. The single most
important line in its prompt is permission to return **nothing** — extraction
over-produces because it asks a leading question, and selection is told that most
conversations hold one or two real commitments and that keeping none is a correct answer.

Measured across 14 recordings, on the thing the owner's own drop history says he rejects
— actions that describe the conversation rather than work arising from it:

| | meta-talk rate | n |
|---|---|---|
| all candidates | 12.0% | 565 |
| first-3 by extraction order | **21.2%** | 33 |
| selection | **0.0%** | 28 |

Taking the first three is *worse than average*, because meta-talk clusters early in a
conversation.

**Nothing is deleted.** Everything below the line becomes `overflow` — off the board,
kept, carrying the model's reason, and promotable in one click. A selection that fails to
parse keeps everything and says so, because "none of these are real" and "the model did
not answer" are different facts.

### Is the right three on the board?

Nobody knows yet, and the honest answer matters more than a confident one. The meta-talk
number above is a proxy; agreement with what you actually keep is the real question, and
the archive cannot answer it — seven accepted actions, and 539 of 567 drops arriving in
bursts of ten or more, which is a person clearing a board rather than judging items.

```bash
plaudctl judge              # label a sample; items shown in random order
plaudctl judge --measure    # precision and recall, selection vs extraction order
plaudctl judge --status     # how much is labelled
```

It labels **the whole candidate pool** for each sampled recording, not the ranker's
picks. That is the difference between a set that measures any future ranker offline
forever and one that rots the moment the ranker changes. Recall is reported beside
precision, because precision alone makes "keep nothing" look perfect.

Verdicts live in `{archive_root}/eval/actions.jsonl`, never in this repository.

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

### Conversations that specify something

A conversation where you spec something for an agent to build gets a **brief**, not a
longer checklist. Sixty checkboxes is not a specification — it is a specification
shredded into sixty pieces, each of which has lost the context that made it mean
anything.

```bash
plaudctl kinds --set <id> product   # confirm it: a person decides this
plaudctl brief                      # write one for each confirmed conversation
plaudctl brief --show <id>
```

**A brief needs your confirmation, not just the classifier's.** The first real run wrote
one for a recording classified `product` at 0.95 confidence that turned out to be mostly
a personal argument with some business talk in it — the summary the classifier read
genuinely was business analysis. A brief is the one artifact here designed to travel into
an agent's context, so it follows the same rule as everything else: a proposal does
nothing until a person accepts it. The classifier proposes; `kinds --set` confirms.

Five sections: **Intent**, **Constraints**, **Decided**, **Open**, **Risks** — each
citing the timestamps it came from. `## Open` is the section that justifies the document:
a model asked to summarise a design conversation reports the decisions and quietly drops
the disagreements, because decisions sound like conclusions and open questions sound like
noise. An agent acting on the decisions while unaware of what is unresolved is exactly
the failure this prevents, so the section is asked for explicitly, "None stated" has to be
written out, and the merge step is told that the only reason to drop an open question is
that the conversation resolved it — in which case it moves to `## Decided`.

**Edit them.** A brief is a working document and a re-run will never overwrite one you
have touched, `--force` included. The marker that says "generated" lives inside the file,
not in the database, because the file is the artifact: it gets copied, mailed, and pasted
into an agent's context, and provenance stored elsewhere stops travelling with it.

Agents fetch it with the `get_brief` MCP tool.

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

Each hit carries two texts, and they are not interchangeable. `passage` is the indexed
chunk sitting at `at` — the only text attributable to that timestamp, and the one a
client is told to quote. `context` is that passage stitched with its neighbours (one
either side by default, `context=0` to switch it off), spanning `context_span`. It is
there so a client can understand what was being discussed without a second
`get_transcript` call and a guessed time window. Widening `passage` itself would have
been simpler and wrong: clients would keep quoting the field and start attributing a
neighbour's sentence to a moment where it was never said.

**Constraints are arguments, not prose.** `search_recordings` takes `period` and
`speaker`; `list_actions` takes `period`, `kind` and `owner`. Both apply them before
ranking, so an agent asking about March gets March:

```jsonc
list_actions(period="March 2026", kind="commitment", status="accepted")
// → rows with text, owner, due date, the verbatim quote, the recording and
//   timestamp to check it against, and `dispatchable` — which is false unless a
//   human accepted it.
```

That is the tool for "what did I commit to in March". Similarity search cannot honour a
date, and an agent that asks it to will get a confident answer drawn from the wrong
months.

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
plaudctl search "what did I commit to" --period "August 2026"
plaudctl search "the schema argument" --speaker Chidera
plaudctl search "the schema argument" --context 1   # print around each hit
```

Or the **Search** tab in the console, where every hit opens the recording cued to the
moment it was said.

**State a constraint as a flag, not inside the query.** `--period` ("March",
"March 2026", "2026-03-14", "last 30 days") and `--speaker` are applied in SQL before
anything is ranked. Written into the query text instead they do nothing: asked
*"what did I commit to in August"* as free text, this returned a September recording,
because "August" is compared as meaning rather than read as a bound.

For commitments and tasks specifically, reach for the action board rather than search —
those live in a table with dates and owners, and similarity is the wrong instrument for
a question that is really a filter.

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

**Reading around a hit.** A passage is ~1200 characters, sized so a hit points at a
findable moment rather than "somewhere in these ten minutes". That is the wrong size for
answering *from*, so `--context N` prints the N passages either side, stitched — the
overlap that chunks share is removed, because a window that says the same sentence twice
reads as emphasis that was never there. The hit itself is still printed above, unchanged
and at its own timestamp: the passage at `[00:14:22]` is the thing you may quote as
having been said at 00:14:22, and the window around it is not.

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

- **Oblique retrieval is roughly a coin flip.** Measured over 48 generated queries:
  naming a person, company or number finds the right recording first 79% of the time;
  describing the same thing in words the recording did not use, 55%. The second number is
  the one that matters, because it is the case semantic search exists for.
- **A long recording is not one conversation, and everything assumes it is.** A pin left
  running produces one file holding several unrelated conversations; a 2.9-hour file here
  contains a compliance audit, a job interview and two introductions. One title, one
  summary and one tone score are stretched across all of it, and its chunk count puts it
  near the top of almost every search.
- **The console can serve a newer page than its own code.** `index.html` is read from disk
  each request; the Python is imported once. A console left running across an edit serves
  the new page against the old endpoints, and the symptom is a feature that 404s rather
  than anything resembling a stale process. It now detects this and says so —
  `plaudctl service restart` is the fix.
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

## Making a run faster

Profiled rather than guessed. On an M4 Pro, prompt processing is nearly free — 5,634
prompt tokens cost under a second — and generation runs at about 30 tokens a second.
**The whole cost of a call is the length of what the model writes.**

Which makes the biggest win a prompt change, not hardware. Extraction was asked for an
unlimited list and returned 55 candidates from one chunk, for a conversation whose budget
is 3:

| one 12,000-character chunk | time | output | items |
|---|---|---|---|
| uncapped | 138s | 2,400 tokens | 55 (or 0, truncated) |
| `AT MOST 8 items` | **28s** | 398 tokens | 8 |

A five-chunk recording still offers 40 candidates for 3 places and selection sees all of
them at once, so this is not a recall cut — it is declining to pay for candidates that
exist only to be discarded. Extrapolated over this archive's 361 chunk-calls, an
extraction pass goes from about 14 hours to under 3.

**What did not help, measured.** Running the calls concurrently is the obvious next move
and does nothing locally: generation is memory-bandwidth bound, not latency bound. One
stream of an 8B at Q4 reads 5.2 GB of weights thirty times a second — 156 GB/s of an M4
Pro's 273 GB/s — so a second stream has nowhere to run. On a real transcript with the
model warm: 65.8s serial, 66.9s with two workers. `llm_workers` therefore defaults to 1,
and rises only for a hosted `cloud_model`, where the wait is network latency against a
provider running its own parallelism.

**Worth setting on the machine**, none of which this project can set for you:

```bash
export OLLAMA_FLASH_ATTENTION=1   # faster attention, smaller KV cache
export OLLAMA_KEEP_ALIVE=30m      # default 5m — stages reload the model between them
```

And check which model you are actually on. `qwen3:latest` answers a classification in
4.4s here; `qwen3.5:latest` takes **88s** for the same prompt. The second is the
shipped default in `config.py`, so a fresh install is twenty times slower until you
change it.

## Using a bigger model, on demand

Everything defaults to local. Two steps can be pointed at a large model when you want
one, because they are the two where judgement beats volume:

```toml
cloud_model      = "gpt-oss:120b-cloud"   # or any model on an OpenAI-compatible endpoint
cloud_tier_scope = "stack"                # which tiers may leave. Empty = none.
```

```bash
plaudctl extract --cloud    # extract locally, CHOOSE with the big model
plaudctl brief --cloud      # write the brief with the big model
```

`extract --cloud` keeps the transcript on the machine: extraction is ~15 calls per
recording and stays local, while selection is **one** call that sends only the candidate
list and the summary. `brief --cloud` does send the transcript, which is why it is gated
per recording. Everything else — summaries, tone, titles, embeddings — stays local
regardless.

### What actually leaves, and what does not

**Anything you send to a hosted model leaves your machine.** That is true of Ollama's
cloud, OpenAI, Groq, OpenRouter and everyone else, whatever their retention policy says.
Read the current terms of whichever you pick; this project cannot make a promise on
another company's behalf, and a policy is a promise rather than a mechanism.

What this project does instead is make the decision explicit and per tier:

- `cloud_tier_scope` is **empty by default**. Setting `cloud_model` on its own sends
  nothing — holding a key and deciding which conversations may leave are two decisions,
  and one switch for both is how a therapy session reaches a vendor.
- A tier outside the scope **raises**. It does not quietly fall back to the local model,
  because two models' work on one board with nothing saying which wrote what is its own
  kind of lie.
- **Embeddings can never be hosted.** Indexing sends every sentence in the archive in
  one sweep, so there is no per-recording decision to gate and no scope that makes it
  proportionate. It is refused outright.

**One trap worth knowing about, now closed.** Ollama's hosted models are pulled like any
other and addressed at `127.0.0.1:11434` — the local daemon forwards the prompt to
Ollama's servers. An address check therefore reported *"nothing leaves this machine"*
while it did, and the tier gate never engaged. A `-cloud` suffix now makes a model remote
regardless of the address it is dialled at.

## Clearing recordings out of Plaud's cloud

Deletion is never automatic and never a side effect. It is two decisions, made
separately: *mark* the recordings you no longer want stored on their servers, then *run*
the thing that talks to them.

**Marking**, in the console — select recordings, then **mark for cloud deletion** in the
selection bar. Or from the terminal, for the case where you mean "all of them":

```bash
plaudctl mark --eligible          # dry run: shows what would be queued
plaudctl mark --eligible --yes
plaudctl mark --eligible --unmark # changed your mind
```

Marking changes nothing on Plaud and nothing about what a recording *is* — a `stack`
recording stays `stack`. It only says you are done paying someone else to keep the audio.

**Pruning**, which is the part that leaves your machine:

```bash
plaudctl prune --probe --yes      # trashes exactly ONE and verifies it landed
plaudctl prune                    # dry run over the rest
plaudctl prune --yes
```

The probe is mandatory the first time. Plaud's trash endpoint is **inferred, not
documented**, so bulk pruning stays locked until one recording has proved the endpoint
behaves and a receipt says so. Delete the receipt and you are back to probe-only.

Every precondition is re-checked at prune time, not trusted from when you marked it: the
local audio must still hash to what was recorded at download, still have a transcript and
a vault note, and be older than `prune_min_age_days`. And it uses **trash**, not hard
delete — recoverable from the Plaud app for a window.

Your local archive is never touched by any of this. It is the copy you are keeping.

## Developing

```bash
pip install -e '.[dev]'
pytest                  # ~280 tests, a second or two
ruff check plaudvault tests scripts
python scripts/render-diagrams.py
```

The suite needs **no network, no Ollama and no archive** — every test builds its own
SQLite in a tmpdir — so it runs anywhere and runs in CI on every push and pull request.

It is aimed at the properties that would fail *silently*: the tier scope an MCP client
reads through, `exclude` being unreachable on every path, the check that an extracted
quote actually appears in its transcript, filters applied before ranking rather than
after, and the stitching behind `--context`. Those are the behaviours where a regression
looks exactly like a correct answer. `web.py`, `cli.py` and `story.py` have no tests;
changes there are checked by running them.

The diagrams are built, not exported. `python scripts/render-diagrams.py` renders
`docs/diagrams/*.excalidraw` to PNG, so a picture cannot quietly stop describing the
schema it claims to. They were hand-exported once, which is fine once and a liability
forever.

The linter's rule set is deliberately narrow — unused imports, shadowed names, obvious
bug shapes — because this codebase argues for itself in prose and a linter with opinions
about prose is noise. Its first run found a `NameError` that a broad `except` around a
batch loop had been reporting as an ordinary per-recording failure for weeks.

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
