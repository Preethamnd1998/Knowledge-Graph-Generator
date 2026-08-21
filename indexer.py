#!/usr/bin/env python3
"""Code knowledge graph indexer for microservices.

Usage:
    python indexer.py

Reads every setting from kg-config.env (next to this file, or pointed to by
the KG_CONFIG_PATH environment variable) — no command-line arguments. To
change what gets indexed, edit kg-config.env: comment a line with '#' to
exclude a service/subproject, remove the '#' to include it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kg_indexer.config import ConfigError, load_config
from kg_indexer.orchestrator import run

DEFAULT_CONFIG_NAME = "kg-config.env"


def main():
    config_path = os.environ.get(
        "KG_CONFIG_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), DEFAULT_CONFIG_NAME),
    )
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        run(config)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
