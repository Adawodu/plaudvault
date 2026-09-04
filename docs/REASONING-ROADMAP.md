# Asking the archive a question, and having something happen

What it takes to go from *"semantic search returns cited passages"* to *"list my
commitments from March and put them on my calendar"* — measured against what is
actually built, using numbers from the live archive rather than intuition.

> Companion to [PRODUCT-BIBLE.md](PRODUCT-BIBLE.md), which records what exists and why.
> This one records what does not exist yet and in what order it is worth building.
> Where they disagree, the bible wins.

---

## 1. The demonstration

`plaudctl search "what did I commit to in August"`, run against the live archive on
2026-09-03:

| Rank | Recording | Date |
|---|---|---|
| 1 | Jadara and Chidara parenting talk | 2026-08-20 |
| 2 | Amy and Skyla's Date Night Planning | 2026-08-23 |
| 3 | Bayo's running and job search goals | **2026-09-01** |
| 4 | Jadara and Chidara parenting talk | 2026-08-20 |

A September recording in an answer about August, and four passages that are not
commitments. Neither constraint in the question did any work: "August" was embedded as
*meaning* and compared against text, and "commit to" likewise.

The same question as a query over the tables — recordings whose `started_at` falls in
August, joined to actions of `kind = 'commitment'` — returns **71 rows**, correctly
dated, most carrying an owner.

**The archive holds the answer. Similarity search is not the instrument that can find
it.** That is the whole of section 3.

---

## 2. Three problems that look like one

"Add a reasoning model so I can ask questions and get accurate answers" is three asks,
and they need different things.

### Synthesis — largely already solved, and not the bottleneck

Bible §9 settled that retrieval belongs here and synthesis belongs in whatever asked;
B1 ("ask with citations" inside plaudvault) was dropped for exactly that reason. When an
agent queries over MCP, **its model is already the reasoning model.** Nothing needs to be
added inside plaudvault to make questions answerable.

What *is* wrong is the ingredients handed to that model: unfiltered retrieval (§3) and a
commitment board that is one percent trustworthy (§4). A better model reasoning over bad
retrieval produces a confident wrong answer, which is the failure mode the eval harness
was built to catch and the one worth fearing here.

### Retrieval — cannot express a constraint

`search()` takes a query string and nothing else. No date range, no speaker, no tier, no
kind. Every question containing a structured constraint degrades into similarity over
the entire corpus. This is B5, and §1 is what it looks like.

### Extraction — the actual blocker

Numbers from the live archive:

| | |
|---|---|
| Commitments extracted | 460 |
| Dropped on review | **346** |
| Awaiting review | 109 |
| Ever accepted or completed | **5** |
| Carrying a due date | **4 of 681 actions** |

Three quarters of what the extractor proposed was thrown away. D20 requires a human to
accept an action before an agent can be dispatched it — correctly, because the
acceptance step *is* the human reading the quote. That gate makes extraction precision
the hard floor under every downstream automation: an agent cannot make a calendar invite
out of a board nobody trusts enough to accept from.

**The dispatch half is built and idle.** `my_tasks`, `claim_task` and `report_task`
already give an agent a queue, a claim so two agents cannot take the same work, and a
report that does not close the action. The pipe to an agent is finished. There is
nothing worth putting through it.

---

## 3. Where a cloud model actually helps

Not retrieval. Embeddings are a separate model and stay local regardless; a larger chat
model does not improve a vector search.

**Extraction.** Telling a commitment from rhetoric is a judgment task, it is where
qwen3:8b measurably fails (B8), and it runs offline in batch — which means latency and
cost barely matter and nothing is blocked while it runs. D14 established that a larger
*local* model physically cannot load on 24 GB; a remote one sidesteps that without
touching the constraint.

The provider hook already exists: `llm_provider = "openai"` targets any OpenAI-compatible
`/chat/completions` endpoint, so this is configuration, not construction.

**There is a free labelled dataset waiting.** 346 commitments this archive's owner
explicitly rejected, and 5 accepted. Re-running extraction with a larger model over the
same recordings and scoring it against those decisions is a real precision measurement,
available today, with no annotation work. It is also the honest test of D-open-2: either
the board gets materially better, or the ambition is retired and Actions becomes
"moments worth revisiting".

### The decision that has to come first

Sending a transcript to a cloud model breaks the property the product is built on.
`llm.is_local()` already drives a warning — `remote — transcripts leave this machine` —
but it is **global**. It does not know that `stack` is work and `local` is a marriage.

This archive holds 62 recordings in which therapy sessions, arguments with a spouse and
conversations in front of children sit beside compliance meetings. "Temporarily use a
cloud model" cannot mean all of it, and a global switch is one careless run away from
meaning exactly that.

The fix has an obvious shape, because D21 already solved the same problem for a
different client: a **tier scope on the remote provider**. Work recordings may reach a
remote model; family conversations physically cannot, enforced where every other tier
decision is enforced rather than by remembering. This should land before an API key
does, not after.

---

## 4. The gaps, in order

Ordered by value over effort, not by interest.

| # | Gap | Why it is first / later |
|---|---|---|
| ~~1~~ | ~~Filters ahead of vector search~~ | **Shipped** — D26. `since`/`until`/`tiers`/`speaker` applied in SQL before ranking, in `search()`, the CLI and MCP. |
| ~~2~~ | ~~A structured query over the actions table~~ | **Shipped** — D26. `list_actions(period=, kind=, owner=)`, rows carrying quote, timestamp and a `dispatchable` flag. |
| ~~3~~ | ~~A remote-provider tier scope~~ | **Shipped** — D27. `cloud_tier_scope`, empty by default, refusing rather than falling back. |
| **4** | **Extraction against a larger model** | The blocker under all automation, now measurable against 346 human rejections. |
| **5** | **Due-date resolution during extraction** | "By Friday" has to become a date at extraction time or every calendar invite needs a human to retype it. 4 of 681 actions carry one today. |
| **6** | **Neighbour expansion** (B4) | A 1200-char chunk is a search snippet. An agent answering a question wants the hit and its neighbours. |
| **7** | **A verified golden set** (B14) | Everything above is a change to retrieval or extraction quality, and none of it can be shown to have helped without it. |

Items 1, 2 and 3 shipped 2026-09-03 and needed no model and no key. The
`commitments in period → agent` path now works end to end for commitments a human has
accepted; measured against the live archive, "commitments recorded in August" returns
100 correctly-bounded rows of which **exactly one is dispatchable**, because one is all
that has ever been accepted.

That number is the whole argument for what comes next. The mechanism is finished and the
board is empty, so items 4 and 5 — a larger model on extraction, scored against the 346
commitments already rejected, and due dates resolved while the transcript is still in
front of the model — are what make the path worth walking.

---

## 5. What "done" looks like

An agent asks plaudvault for commitments in a period, gets rows with dates, owners,
quotes and recording citations, and puts the accepted ones on a calendar. Concretely:

1. **The agent asks a structured question.** `list_actions(kind="commitment",
   since="2026-03-01", until="2026-04-01")` — not a similarity search that happens to
   surface commitments.
2. **The rows carry what a calendar needs**: text, owner, due date, the verbatim quote,
   the recording and timestamp to check it against.
3. **Only accepted actions are dispatchable** (D20, unchanged). Proposals are visible to
   the agent and refuse to be acted on.
4. **The agent reports back rather than closing anything** (`report_task`, unchanged) —
   plaudvault records what the agent said it did and cannot verify it, so the action
   stays open until a human closes it.
5. **Tier is enforced once**, on both the MCP scope and the remote model, so neither the
   agent nor the cloud provider can reach a recording the tier excludes.

Steps 3, 4 and 5 are built. Step 1 needs items 1 and 2 above. Step 2 needs item 5.
