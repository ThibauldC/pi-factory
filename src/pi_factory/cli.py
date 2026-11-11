"""Command line entry point: ``pi-factory run`` and ``pi-factory check``."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from . import logs, preflight
from .config import ConfigError, load_config
from .secrets import SecretsError, load_secrets
from .service import build_services, report, run_service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-factory",
        description="Turn GitHub issues into reviewed pull requests by driving pi.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("run", "run the factory (preflight checks first)"),
        ("check", "run the preflight checks and exit"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--config", default=None, help="path to factory.toml")
        p.add_argument("--secrets", default=None, help="path to secrets.env")
    return parser


def main(argv: list[str] | None = None, **service_overrides: Any) -> int:
    args = build_parser().parse_args(argv)
    logs.setup_logging()
    try:
        cfg = load_config(args.config)
        secrets = load_secrets(args.secrets)
    except (ConfigError, SecretsError) as exc:
        print(f"pi-factory: {exc}", file=sys.stderr)
        return 1
    services = build_services(cfg, secrets, **service_overrides)
    if args.command == "check":
        deps = services.deps
        results = preflight.run_checks(cfg, secrets, deps.tracker, deps.codehost)
        return 0 if report(results) else 1
    return run_service(services)


if __name__ == "__main__":
    raise SystemExit(main())
