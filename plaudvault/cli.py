"""plaudctl — own your Plaud recordings end to end."""

from __future__ import annotations

import argparse
import sys
import time

from . import (auth, extract, freshness, notes, prune, runlock, sentiment,
               service, setup_wizard, summarize, sync, tiering, transcribe)
from .api import PlaudClient
from .config import ArchiveUnavailable, load
from .store import Store


def _client(cfg):
    return PlaudClient(auth.require_token(cfg), cfg.api_base)


def cmd_login(args, cfg) -> int:
    auth.login(cfg, args.email)
    cfg = load()
    with _client(cfg) as client:
        devices = client.devices()
    print(f"  connected — {len(devices)} device(s):")
    for d in devices:
        print(f"    {d.get('name')} ({d.get('model')}) sn={d.get('sn')} fw={d.get('version_number')}")
    return 0


def cmd_logout(args, cfg) -> int:
    if cfg.email and auth.keychain_delete(cfg.keychain_service, cfg.email):
        print("  token removed from Keychain")
    else:
        print("  no stored token")
    return 0


def cmd_sync(args, cfg) -> int:
    with _client(cfg) as client, Store(cfg.db_path) as store:
        s = sync.sync(cfg, client, store, limit=args.limit)
    print(
        f"\n  downloaded {s['new']} ({s['bytes'] / 1e9:.2f} GB), skipped {s['skipped']}, "
        f"failed {s['failed']}, unverified {s['unverified']}"
    )
    return 1 if s["failed"] else 0


def cmd_verify(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        s = sync.verify(cfg, store)
    print(f"\n  ok {s['ok']}, missing {s['missing']}, corrupt {s['corrupt']}")
    return 1 if (s["missing"] or s["corrupt"]) else 0


def cmd_transcribe(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        s = transcribe.run(cfg, store, limit=args.limit, force=args.force)
    if s["wall_seconds"]:
        print(
            f"\n  transcribed {s['done']}, failed {s['failed']} — "
            f"{s['audio_seconds'] / 3600:.1f}h audio in {s['wall_seconds'] / 60:.0f}m wall "
            f"({s['audio_seconds'] / s['wall_seconds']:.0f}x realtime)"
        )
    return 1 if s["failed"] else 0


def cmd_summarize(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        s = summarize.run(cfg, store, limit=args.limit, force=args.force)
    print(f"\n  summarized {s['done']}, failed {s['failed']}")
    return 1 if s["failed"] else 0


def cmd_sentiment(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        s = sentiment.run(cfg, store, limit=args.limit, force=args.force)
    print(f"\n  scored {s['scored']}, skipped {s['skipped']} (too short), failed {s['failed']}")
    return 1 if s["failed"] else 0


def cmd_notes(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        s = notes.run(cfg, store, limit=args.limit, force=args.force)
    print(f"\n  wrote {s['written']} notes, failed {s['failed']}")
    return 1 if s["failed"] else 0


def cmd_extract(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        s = extract.run(cfg, store, limit=args.limit, force=args.force)
    print(f"\n  scanned {s['recordings']}, proposed {s['proposed']} actions, failed {s['failed']}")
    print("  review them in the console: plaudctl web")
    return 1 if s["failed"] else 0


def cmd_tier(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        s = tiering.sync(cfg, store)
    print(f"  {tiering.stack_dir(cfg)}")
    print(f"  approved {s['approved']} (+{s['added']} added, {s['updated']} updated, -{s['removed']} removed)")
    return 0


def cmd_init(args, cfg) -> int:
    return setup_wizard.run()


def cmd_service(args, cfg) -> int:
    if args.action == "install":
        hours = [int(h) for h in args.hours.split(",")] if args.hours else None
        return service.install(hours)
    if args.action == "uninstall":
        return service.uninstall()
    return service.status()


def cmd_web(args, cfg) -> int:
    from .web import serve

    serve(port=args.port or cfg.web_port)
    return 0


def cmd_run(args, cfg) -> int:
    """The daily pipeline. Never prunes — that stays a deliberate, separate act."""
    started = time.time()
    try:
        with runlock.acquire(cfg.archive_root, what="run"):
            rc = _run_stages(args, cfg)
    except runlock.RunInProgress as exc:
        print(f"  skipped: {exc}")
        return 0  # not an error — the scheduler colliding with a manual run is normal
    runlock.record_run(cfg.archive_root, started=started, rc=rc)
    return rc


def _run_stages(args, cfg) -> int:
    print("== sync ==")
    rc = cmd_sync(args, cfg)
    print("\n== transcribe ==")
    rc |= cmd_transcribe(args, cfg)
    print("\n== summarize ==")
    try:
        rc |= cmd_summarize(args, cfg)
    except RuntimeError as exc:
        print(f"  [skip] {exc}")
    # Before notes, so the frontmatter carries the reading rather than lagging a run.
    print("\n== sentiment ==")
    try:
        rc |= cmd_sentiment(args, cfg)
    except RuntimeError as exc:
        print(f"  [skip] {exc}")
    print("\n== notes ==")
    rc |= cmd_notes(args, cfg)
    print("\n== extract ==")
    try:
        rc |= cmd_extract(args, cfg)
    except RuntimeError as exc:
        print(f"  [skip] {exc}")
    print("\n== tier sync ==")
    rc |= cmd_tier(args, cfg)
    return rc


def cmd_status(args, cfg) -> int:
    with Store(cfg.db_path) as store:
        c = store.counts()
        rows = store.all()
    from . import llm as _llm

    tr_ok, tr_why = transcribe.backend_status(cfg)
    llm_ok, llm_why = _llm.available(cfg)
    print(f"  archive:  {cfg.archive_root}")
    print(f"  vault:    {cfg.notes_dir or '(none configured)'}")
    print(f"  asr:      {'ok ' if tr_ok else 'XX '}{tr_why}")
    print(f"  llm:      {'ok ' if llm_ok else 'XX '}{cfg.llm_label} — {llm_why}"
          + ("" if _llm.is_local(cfg) else "  [remote: transcripts leave this machine]"))
    print(f"  account:  {cfg.email or '(not logged in)'} @ {cfg.api_base}")
    tok = auth.stored_token(cfg)
    if tok:
        exp = auth.token_expiry(tok)
        print(f"  token:    valid until {time.strftime('%Y-%m-%d', time.localtime(exp)) if exp else '?'}")
    else:
        print("  token:    none — run: plaudctl login <email>")
    print()
    print(f"  recordings known:   {c['total']}")
    print(f"    downloaded:       {c['downloaded']}")
    print(f"    download verified:{c['verified']}   (complete, not truncated)")
    print(f"    byte-identical:   {c['md5_exact']}   (matches Plaud's md5; rest carry an ID3 tag)")
    print(f"    transcribed:      {c['transcribed']}")
    print(f"    summarized:       {c['summarized']}")
    print(f"    tone scored:      {c['scored']}")
    print(f"    noted in vault:   {c['noted']}")
    print(f"    pruned from cloud:{c['pruned']}")
    hours = sum(r["duration_s"] or 0 for r in rows) / 3600
    gb = sum(r["audio_size"] or 0 for r in rows) / 1e9
    print(f"\n  {hours:.1f} hours of audio, {gb:.2f} GB on disk")

    print("\n  freshness:   (plaudctl fresh --cloud also checks Plaud for new recordings)")
    with Store(cfg.db_path) as store:
        _print_freshness(freshness.report(cfg, store))
    if not prune.receipt_path(cfg).exists():
        print("\n  prune: LOCKED (no probe receipt)")
    else:
        print("\n  prune: unlocked")
    return 0


def _print_freshness(report: dict) -> None:
    mark = "ok " if report["up_to_date"] else "-> "
    print(f"  {mark}{report['headline']}")
    for item in report["pending"]:
        print(f"      {item['count']:>4} not yet {item['label']:<28} {item['fix']}")
    if report["missing_notes"]:
        print(f"      {len(report['missing_notes']):>4} notes recorded but gone from the vault"
              "   plaudctl notes --force")
        for n in report["missing_notes"][:5]:
            print(f"           {n['path']}")
    s = report["stack"]
    if s["drifted"]:
        print(f"      stack corpus drifted: +{s['add']} ~{s['update']} -{s['remove']}"
              "        plaudctl tier")
    r = report["remote"]
    if r["checked"]:
        print(f"      cloud holds {r['cloud_total']}, {r['new']} not yet archived here")
        for n in r["newest"][:5]:
            print(f"           {n['started_iso']}  {n['filename'][:50]} ({n['duration_min']} min)")
    elif r.get("detail"):
        print(f"      cloud not checked — {r['detail']}")
    else:
        print("      cloud not checked")
    if report["untriaged"] or report["proposed_actions"]:
        print("\n  waiting on you (not on the pipeline):")
        if report["untriaged"]:
            oldest = report["oldest_untriaged_days"]
            print(f"      {report['untriaged']:>4} recordings untriaged"
                  + (f", oldest {oldest} days" if oldest else ""))
        if report["proposed_actions"]:
            print(f"      {report['proposed_actions']:>4} proposed actions awaiting review")


def cmd_fresh(args, cfg) -> int:
    """Is the vault current? Local always; the cloud only when asked."""
    client = None
    with Store(cfg.db_path) as store:
        try:
            if args.cloud:
                client = _client(cfg)
            report = freshness.report(cfg, store, client=client)
        finally:
            if client is not None:
                client.close()

    last = runlock.last_run(cfg.archive_root)
    if last:
        ago = (time.time() - last["finished"]) / 3600
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last["finished"]))
        print(f"  last full run: {when} ({ago:.1f}h ago, exit {last['rc']})")
    else:
        print("  last full run: never recorded — run: plaudctl run")
    _print_freshness(report)
    if not args.cloud:
        print("\n  add --cloud to also check Plaud for recordings not yet downloaded")
    return 0 if report["up_to_date"] else 1


def cmd_prune(args, cfg) -> int:
    with _client(cfg) as client, Store(cfg.db_path) as store:
        if args.probe:
            return 0 if prune.probe(cfg, client, store, confirm=args.yes) else 1
        s = prune.run(cfg, client, store, confirm=args.yes, limit=args.limit)
    print(f"\n  pruned {s['pruned']}, skipped {s['skipped']}, failed {s['failed']}")
    return 1 if s["failed"] else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="plaudctl", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn, limit=None, force=False, yes=False, probe=False, cloud=False)
        return sp

    sp = add("login", cmd_login, "authenticate with Plaud (emailed one-time code)")
    sp.add_argument("email")

    add("logout", cmd_logout, "remove the stored token from the Keychain")
    add("status", cmd_status, "show archive + pipeline state")

    sp = add("fresh", cmd_fresh, "is the vault up to date? what still needs processing?")
    sp.add_argument("--cloud", action="store_true",
                    help="also ask Plaud for recordings not yet downloaded")

    sp = add("sync", cmd_sync, "download new recordings from the Plaud cloud")
    sp.add_argument("--limit", type=int)

    add("verify", cmd_verify, "re-hash the local archive and flag drift")

    for name, fn, help_ in (
        ("transcribe", cmd_transcribe, "transcribe locally with mlx-whisper"),
        ("summarize", cmd_summarize, "summarize locally with Ollama"),
        ("sentiment", cmd_sentiment, "score the tone of transcripts (feeds the trend chart)"),
        ("notes", cmd_notes, "write/refresh Obsidian notes"),
        ("extract", cmd_extract, "propose actions from transcripts (review in the console)"),
    ):
        sp = add(name, fn, help_)
        sp.add_argument("--limit", type=int)
        sp.add_argument("--force", action="store_true", help="redo already-processed items")

    add("tier", cmd_tier, "reconcile PLAUD/stack/ with your triage decisions")

    sp = add("web", cmd_web, "open the console (triage, actions, measures)")
    sp.add_argument("--port", type=int, default=None)

    add("init", cmd_init, "interactive first-time setup")

    sp = add("service", cmd_service, "install/remove the background services")
    sp.add_argument("action", nargs="?", default="status",
                    choices=["install", "uninstall", "status"])
    sp.add_argument("--hours", help="comma-separated sync hours, e.g. 7,12,18,22")

    sp = add("run", cmd_run, "sync -> transcribe -> summarize -> sentiment -> notes -> extract")
    sp.add_argument("--limit", type=int)

    sp = add("prune", cmd_prune, "move locally-archived recordings to Plaud's trash")
    sp.add_argument("--probe", action="store_true", help="verify the endpoint on ONE recording")
    sp.add_argument("--yes", action="store_true", help="actually send it (default is dry run)")
    sp.add_argument("--limit", type=int)

    args = p.parse_args(argv)
    cfg = load()
    # Every verb except login/logout touches the archive volume; fail loudly and
    # early rather than writing a phantom archive onto the boot disk.
    if args.cmd not in ("login", "logout"):
        try:
            cfg.check_archive_available()
        except ArchiveUnavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
    try:
        return args.fn(args, cfg)
    except (auth.PlaudAuthError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
