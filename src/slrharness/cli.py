"""Unified command-line entry point for SLRHarness v1."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from slrharness import orchestrator, scope
from slrharness.config import load_config
from slrharness.diagnostics import doctor, render_report, validate_project


def _diagnostic_command(command: str, argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog=f"slrharness {command}")
    if command == "doctor":
        parser.add_argument("--workspace", type=Path)
        args = parser.parse_args(argv)
        report = doctor(args.workspace)
    else:
        parser.add_argument("project", type=Path)
        args = parser.parse_args(argv)
        report = validate_project(args.project)
    print(render_report(report))
    if not report.ok:
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print(
            "SLRHarness commands:\n"
            "  scope <prepare|resume|show|approve|revise|reject|init> ...\n"
            "  run --workspace PROJECT ...\n"
            "  finalize --workspace PROJECT ...\n"
            "  status PROJECT\n"
            "  validate PROJECT\n"
            "  config FILE\n"
            "  doctor [--workspace PATH]"
        )
        return
    command, rest = argv[0], argv[1:]
    if command == "scope":
        scope.main(rest)
    elif command in {
        "prepare",
        "resume",
        "show",
        "approve",
        "revise",
        "reject",
        "init",
    }:
        scope.main([command, *rest])
    elif command in {"run", "finalize"}:
        orchestrator.main([command, *rest])
    elif command in {"doctor", "validate", "status"}:
        _diagnostic_command("validate" if command == "status" else command, rest)
    elif command == "config":
        parser = argparse.ArgumentParser(prog="slrharness config")
        parser.add_argument("file", type=Path)
        args = parser.parse_args(rest)
        print(json.dumps(load_config(args.file).as_dict(), indent=2))
    else:
        raise SystemExit(f"unknown command: {command}; run `slrharness --help`")


if __name__ == "__main__":
    main()
