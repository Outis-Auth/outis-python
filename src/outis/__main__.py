"""``python -m outis``: write a worker starter, or generate an intent key."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

from ._intent import generate_key
from ._templates import RUNTIMES, render


def init_worker(runtime: str, directory: Path) -> list[Path]:
    """Write the starter for ``runtime`` into ``directory``. Refuses if any file exists."""
    files = RUNTIMES[runtime]
    targets = {directory / name: content for name, content in files.items()}
    existing = [p for p in targets if p.exists()]
    if existing:
        raise FileExistsError(", ".join(str(p) for p in existing))
    for path, content in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as fh:
            fh.write(render(content, path.stem))
    return list(targets)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m outis")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="write a starter into the current directory")
    init.add_argument("what", choices=["worker"])
    init.add_argument("-runtime", "--runtime", dest="runtime", choices=sorted(RUNTIMES), default="plain")
    init.add_argument("-dir", "--dir", dest="dir", type=Path, default=Path("."), help="where to write (default: here)")
    sub.add_parser("keygen", help="print a new OUTIS_INTENT_KEY")
    args = parser.parse_args(argv)

    if args.command == "keygen":
        print(generate_key())
        return 0
    try:
        written = init_worker(args.runtime, args.dir)
    except FileExistsError as exc:
        print(f"refusing to overwrite: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
