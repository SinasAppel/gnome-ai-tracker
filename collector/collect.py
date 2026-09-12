"""GNOME AI Tracker collector entry point.

Launched asynchronously by the Shell extension (one process per provider,
so a slow or failing provider can never hide another provider's results):

    python3 collect.py --provider claude [--force | --limits-only]
    python3 collect.py --provider codex  [--force | --limits-only]

Prints exactly one normalized, display-safe JSON snapshot to stdout.
Transcript scanning and provider requests stay outside the Shell process;
no daemon is needed. Exit status is 0 on a well-formed snapshot even when
limits are unavailable — that state travels inside the record
(`usageStatusText` / `authHelpText`) so the UI can render it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gnome_ai_tracker import claude, codex  # noqa: E402

PROVIDERS = {"claude": claude, "codex": codex}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gnome-ai-tracker-collect")
    parser.add_argument("--provider", choices=sorted(PROVIDERS), required=True)
    parser.add_argument("--force", action="store_true", help="rescan and re-probe, ignoring caches")
    parser.add_argument(
        "--limits-only",
        action="store_true",
        help="reuse any recent local scan; only the limits probe must be fresh",
    )
    parser.add_argument("--cache-seconds", type=float, default=20)
    parser.add_argument("--claude-dir", default="")
    parser.add_argument("--codex-home", default="")
    parser.add_argument("--codex-bin", default="")
    args = parser.parse_args(argv)

    if args.provider == "claude":
        return claude.main(
            [
                *(["--force"] if args.force else []),
                *(["--limits-only"] if args.limits_only else []),
                "--cache-seconds",
                str(args.cache_seconds),
                *(["--claude-dir", args.claude_dir] if args.claude_dir else []),
            ]
        )
    return codex.main(
        [
            *(["--force"] if args.force else []),
            *(["--limits-only"] if args.limits_only else []),
            *(["--codex-home", args.codex_home] if args.codex_home else []),
            *(["--codex-bin", args.codex_bin] if args.codex_bin else []),
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
