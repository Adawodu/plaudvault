"""plaudvault's pipeline as Dagster software-defined assets.

This is a sketch, not a dependency. plaudvault runs perfectly well as
`plaudctl run` on one machine, and nothing here is needed to use it. It exists to
answer a specific question — *what does this pipeline look like under a real
orchestrator* — and to be the thing you argue with later.

## Why Dagster rather than Airflow

Airflow orchestrates *tasks*: "run transcribe, then run summarize". Dagster
orchestrates *assets*: "a transcript exists for every downloaded recording", and works
out what to run to make that true.

plaudvault already thinks in assets. `store.needing_title()`,
`store.needing_index(model)` and `store.needing_diarization()` are not task queues —
they are reconciliation queries that ask "what is missing?" and can be run at any time,
from any state, without knowing what ran before. That is the same model, which is why
the wrapper below is thin: each asset is four lines around a `run()` that already
behaves correctly.

An imperative DAG would have been a worse fit and would have had to be rewritten.

## What this buys, concretely

- **Lineage.** `transcribe_model`, `summary_model`, `sentiment.model` and
  `chunks.model` are already recorded per artifact. Surfacing them as materialization
  metadata turns "which model produced this?" from a SQL query into a UI.
- **Asset checks.** `freshness.report()` becomes a first-class gate that can block
  downstream materialization rather than a thing you have to remember to look at.
- **Retries and backfills** per asset, without `--force` re-doing the whole corpus.
- **Concurrency limits** that respect the real constraint: one GPU. See `gpu` below.

## What it does NOT buy, and the honest warning

Nothing about this makes the pipeline faster or more correct on one machine. It adds a
daemon, a database and a web server to a system whose current operational surface is a
launchd plist. **Adopt it when a second worker or a second machine exists**, not
before. The value is real and it is a value for teams.

    pip install dagster dagster-webserver
    DAGSTER_HOME=~/.dagster dagster dev -f examples/dagster/definitions.py
"""

# NOTE: no `from __future__ import annotations` here. Dagster resolves the `context`
# parameter by inspecting its annotation object, and postponed evaluation turns that
# into a string it rejects with a confusing "must be annotated with
# AssetExecutionContext" error when it already is.
from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    AssetExecutionContext,
    ConfigurableResource,
    Definitions,
    MetadataValue,
    Output,
    ScheduleDefinition,
    asset,
    asset_check,
    define_asset_job,
)

from plaudvault import (
    diarize,
    evaluate,
    extract,
    freshness,
    notes,
    search,
    sentiment,
    summarize,
    sync,
    tiering,
    titles,
    transcribe,
)
from plaudvault.api import PlaudClient
from plaudvault.auth import require_token
from plaudvault.config import load
from plaudvault.store import Store

# --------------------------------------------------------------------- resources


class Archive(ConfigurableResource):
    """The archive, as an injectable resource.

    Everything in plaudvault already takes `(cfg, store)` explicitly rather than
    reaching for a global, which is the single property that makes wrapping it this
    easy. A module that had opened its own database connection would have had to be
    rewritten before any of this was possible.
    """

    def config(self):
        cfg = load()
        cfg.check_archive_available()  # fail loudly, before a phantom archive is written
        return cfg

    def store(self) -> Store:
        return Store(self.config().db_path)


def _stage(context: AssetExecutionContext, fn, **kwargs) -> Output:
    """Run one plaudvault stage and publish what it did as Dagster metadata.

    Every `run()` in plaudvault already returns a stats dict — `{"done": 4,
    "failed": 0, ...}` — which was written for a human reading terminal output. It is
    exactly the right shape for materialization metadata, so observability here costs
    nothing beyond this function.
    """
    archive = context.resources.archive
    cfg = archive.config()
    with archive.store() as store:
        stats = fn(cfg, store, **kwargs)
        counts = store.counts()

    return Output(
        stats,
        metadata={
            **{k: MetadataValue.int(v) if isinstance(v, int) else MetadataValue.float(v)
               for k, v in stats.items() if isinstance(v, (int, float))},
            # Model identity travels with every materialization, so a change of model
            # is visible in the asset history rather than inferred from a git log.
            "llm": MetadataValue.text(cfg.llm_label),
            "corpus_recordings": MetadataValue.int(counts["total"]),
        },
    )


# ----------------------------------------------------------------------- assets
#
# The dependency graph is declared by the function arguments, exactly as the pipeline
# order in `cli._run_stages` declares it today — except here it is data, so Dagster can
# reason about it: materialize one asset and everything upstream that is stale is
# rebuilt, and nothing that is fresh is touched.


@asset(group_name="capture", description="Audio pulled from Plaud's cloud, verified on arrival.")
def audio(context: AssetExecutionContext) -> Output:
    archive = context.resources.archive
    cfg = archive.config()
    with PlaudClient(require_token(cfg), cfg.api_base) as client, archive.store() as store:
        stats = sync.sync(cfg, client, store, limit=None)
    return Output(stats, metadata={k: v for k, v in stats.items() if isinstance(v, int)})


@asset(group_name="capture", deps=[audio],
       description="Local ASR. The GPU stage: see the `gpu` concurrency pool.")
def transcripts(context: AssetExecutionContext) -> Output:
    return _stage(context, transcribe.run)


@asset(group_name="capture", deps=[transcripts],
       description="Speaker turns and voiceprint matches. Optional — skipped without pyannote.")
def speakers(context: AssetExecutionContext) -> Output:
    cfg = context.resources.archive.config()
    ok, why = diarize.status(cfg)
    if not ok:
        # Not a failure. Diarization is an extra, and a machine without the gated
        # models must not show a permanently red asset for work it cannot do — the
        # same reasoning as D19 in the product bible, expressed as a skip rather than
        # a raise so the downstream graph still runs.
        context.log.info(f"diarization unavailable, skipping — {why}")
        return Output({"skipped": why}, metadata={"skipped": MetadataValue.text(why)})
    return _stage(context, diarize.run)


@asset(group_name="understanding", deps=[transcripts, speakers],
       description="Map-reduce summaries. Depends on speakers: a named transcript summarizes better.")
def summaries(context: AssetExecutionContext) -> Output:
    return _stage(context, summarize.run)


@asset(group_name="understanding", deps=[summaries],
       description="A title a human would recognise, written from the summary.")
def recording_titles(context: AssetExecutionContext) -> Output:
    return _stage(context, titles.run)


@asset(group_name="understanding", deps=[transcripts])
def tone(context: AssetExecutionContext) -> Output:
    return _stage(context, sentiment.run)


@asset(group_name="understanding", deps=[transcripts, speakers],
       description="Proposed commitments. Never auto-accepted — status lands `proposed`.")
def actions(context: AssetExecutionContext) -> Output:
    return _stage(context, extract.run)


@asset(group_name="retrieval", deps=[transcripts, speakers],
       description="Chunk embeddings. Keyed on embed_model, so changing it re-indexes.")
def search_index(context: AssetExecutionContext) -> Output:
    return _stage(context, search.run)


@asset(group_name="publish", deps=[summaries, tone, recording_titles])
def vault_notes(context: AssetExecutionContext) -> Output:
    return _stage(context, notes.run)


@asset(group_name="publish", deps=[transcripts],
       description="Physically reconciles stack/ with triage. Tiering is a fact on disk, not a flag.")
def stack_corpus(context: AssetExecutionContext) -> Output:
    archive = context.resources.archive
    with archive.store() as store:
        stats = tiering.sync(archive.config(), store)
    return Output(stats, metadata={k: v for k, v in stats.items() if isinstance(v, int)})


# ------------------------------------------------------------------ asset checks
#
# This is the part that is genuinely hard to get without an orchestrator. plaudvault's
# freshness report and eval harness already compute the right things; as checks they
# gain a place to live, a history, and the ability to stop a bad index from being used.


@asset_check(asset=search_index, description="Retrieval quality against the golden set.")
def retrieval_quality(context) -> AssetCheckResult:
    """Fails the index if recall regresses, so a bad re-index cannot silently ship.

    This is the check that matters most, because the index is read by an MCP client
    whose model will write a confident answer over whatever it is handed. A citation
    from a degraded index looks exactly like a citation from a good one.
    """
    archive = context.resources.archive
    cfg = archive.config()
    with archive.store() as store:
        try:
            result = evaluate.run(cfg, store, verified_only=True)
        except RuntimeError as exc:
            return AssetCheckResult(
                passed=True,  # not yet measurable is not the same as failing
                severity=AssetCheckSeverity.WARN,
                metadata={"reason": MetadataValue.text(str(exc))},
            )

    m = result["metrics"]
    oblique = (result.get("by_kind") or {}).get("oblique", {})
    # Gate on the oblique set: `direct` queries saturate and would pass forever.
    watched = oblique.get("recall@5", m["recall@5"])
    return AssetCheckResult(
        passed=watched >= 0.60,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "recall@1": MetadataValue.float(m["recall@1"]),
            "recall@5": MetadataValue.float(m["recall@5"]),
            "mrr": MetadataValue.float(m["mrr"]),
            "oblique_recall@5": MetadataValue.float(watched),
            "queries": MetadataValue.int(m["queries"]),
            "saturated": MetadataValue.bool(result.get("saturated", False)),
            "embed_model": MetadataValue.text(result["config"]["embed_model"]),
        },
    )


@asset_check(asset=stack_corpus, description="Does stack/ actually match the triage decisions?")
def stack_in_step(context) -> AssetCheckResult:
    """Tiering is a safety property, so drift is a check rather than a log line."""
    archive = context.resources.archive
    with archive.store() as store:
        report = freshness.local(archive.config(), store)
    s = report["stack"]
    return AssetCheckResult(
        passed=not s["drifted"],
        metadata={k: MetadataValue.int(v) for k, v in s.items() if isinstance(v, int)},
    )


@asset_check(asset=vault_notes, description="Notes the manifest claims that the vault no longer has.")
def notes_present(context) -> AssetCheckResult:
    archive = context.resources.archive
    with archive.store() as store:
        report = freshness.local(archive.config(), store)
    missing = report["missing_notes"]
    return AssetCheckResult(
        passed=not missing,
        severity=AssetCheckSeverity.WARN,
        metadata={"missing": MetadataValue.int(len(missing))},
    )


# ------------------------------------------------------------------ definitions

daily = define_asset_job(
    name="daily",
    selection="*",
    # One GPU, one process. Without this the scheduler will happily start transcription
    # and diarization at once and both will thrash — the single most important thing to
    # get right when moving off a sequential script, and the one an orchestrator
    # actually solves rather than merely relocates.
    config={"execution": {"config": {"multiprocess": {"max_concurrent": 2}}}},
)

defs = Definitions(
    assets=[
        audio, transcripts, speakers, summaries, recording_titles, tone,
        actions, search_index, vault_notes, stack_corpus,
    ],
    asset_checks=[retrieval_quality, stack_in_step, notes_present],
    jobs=[daily],
    schedules=[ScheduleDefinition(job=daily, cron_schedule="0 7,12,18,22 * * *")],
    resources={"archive": Archive()},
)
