"""`plaudctl run` calls the stage functions directly, with its own namespace.

That is the whole design — one pass reconciles everything — and it has a sharp edge:
a flag added to one subcommand is not on the namespace `run` builds, so reading it is
an AttributeError that fires *only* inside `run`. Every subcommand works, the suite
passes, and the failure surfaces four hours into a pipeline.

It has already happened once. `--set` was added to `plaudctl kinds`, and `run` died at
the kinds stage after finishing nine summaries, ten titles, thirty tone readings and
thirty vault notes.

So this reads the parser and the source together: every attribute a run-stage reads off
`args` must exist on the namespace `run` produces. No stage has to be remembered, and a
new flag that forgets the defaults list fails here in milliseconds.
"""

from __future__ import annotations

import ast
import pathlib

from plaudvault import cli

STAGE_SOURCE = pathlib.Path(cli.__file__).read_text()


def _run_namespace():
    parser = cli.build_parser() if hasattr(cli, "build_parser") else None
    if parser is None:                       # the parser is built inside main()
        import argparse
        import contextlib
        import io
        holder = {}
        real = argparse.ArgumentParser.parse_args

        def capture(self, argv=None, namespace=None):
            ns = real(self, argv, namespace)
            holder["ns"] = ns
            raise SystemExit(0)

        argparse.ArgumentParser.parse_args = capture
        try:
            with contextlib.suppress(SystemExit), contextlib.redirect_stdout(io.StringIO()):
                cli.main(["run"])
        finally:
            argparse.ArgumentParser.parse_args = real
        return holder.get("ns")
    return parser.parse_args(["run"])


def _stages_called_by_run() -> set[str]:
    tree = ast.parse(STAGE_SOURCE)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("cmd_run", "_run_stages"):
            for n in ast.walk(node):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                        and n.func.id.startswith("cmd_"):
                    names.add(n.func.id)
    return names


def _attrs_read(fn_name: str) -> set[str]:
    tree = ast.parse(STAGE_SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            return {
                n.attr for n in ast.walk(node)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id == "args"
            }
    return set()


def test_run_calls_more_than_one_stage():
    """If this shrinks to nothing the test below is vacuously green."""
    assert len(_stages_called_by_run()) >= 8


def test_every_flag_a_run_stage_reads_exists_on_the_run_namespace():
    ns = _run_namespace()
    assert ns is not None, "could not build the namespace `plaudctl run` uses"
    missing = {}
    for stage in sorted(_stages_called_by_run()):
        absent = sorted(a for a in _attrs_read(stage) if not hasattr(ns, a))
        if absent:
            missing[stage] = absent
    assert not missing, (
        "these flags are read inside `plaudctl run` but are not on its namespace, "
        f"so the run dies at that stage: {missing}. Add them to set_defaults() in "
        "cli.py's add() helper."
    )
