"""Booth's command line. `python -m ytp`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ytp import __version__
from ytp.layout import init_episode
from ytp.rules import SECTIONS, add_rule, load_rules, rules_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ytp",
        description=(
            "Local YouTube editing pipeline for the Scott Mastin channel. "
            "Draft only: this tool never publishes and never changes a listing."
        ),
    )
    parser.add_argument("--version", action="version", version=f"ytp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create the standard episode folders")
    init.add_argument("episode_dir", type=Path)
    init.add_argument("--series", default="ai-survival")
    init.add_argument("--force", action="store_true", help="rewrite episode.yaml")
    init.set_defaults(func=_cmd_init)

    rule = sub.add_parser("rule", help="read or append series rules")
    rule_sub = rule.add_subparsers(dest="rule_command", required=True)

    add = rule_sub.add_parser("add", help="append one note from Scott")
    add.add_argument("--series", required=True)
    add.add_argument("--section", required=True, choices=SECTIONS)
    add.add_argument("--text", required=True)
    add.add_argument(
        "--rules-root",
        type=Path,
        default=None,
        help="directory that contains rules/ (defaults to the repo)",
    )
    add.set_defaults(func=_cmd_rule_add)

    listing = rule_sub.add_parser("list", help="print rules for a series")
    listing.add_argument("--series", required=True)
    listing.add_argument("--section", choices=SECTIONS)
    listing.add_argument("--rules-root", type=Path, default=None)
    listing.set_defaults(func=_cmd_rule_list)
    return parser


def _cmd_init(args: argparse.Namespace) -> int:
    manifest = init_episode(args.episode_dir, args.series, force=args.force)
    print(f"episode layout ready: {args.episode_dir}")
    print(f"manifest: {manifest}")
    return 0


def _cmd_rule_add(args: argparse.Namespace) -> int:
    root = args.rules_root
    rule = add_rule(args.series, args.section, args.text, root=root)
    print(f"added {rule.id} to {rules_path(args.series, root)}")
    return 0


def _cmd_rule_list(args: argparse.Namespace) -> int:
    rules = load_rules(args.series, args.rules_root)
    names = [args.section] if args.section else list(rules.sections)
    for name in names:
        owner = rules.owners.get(name, "")
        print(f"## {name} ({owner})")
        for rule in rules.sections[name]:
            print(f"{rule.id}: {rule.text}")
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (FileNotFoundError, ValueError) as exc:
        print(f"ytp: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
