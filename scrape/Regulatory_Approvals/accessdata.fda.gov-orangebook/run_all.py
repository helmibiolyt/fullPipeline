#!/usr/bin/env python3
"""Orchestrator: run the Orange Book collector's steps in order.

orangebook_downloader.py is a multi-command tool (download -> parse -> unified).
The pipeline needs the full sequence to produce the analytical CSVs, so this
wrapper runs each command in turn and stops on the first failure.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "orangebook_downloader.py"
STEPS = ["download", "parse", "unified"]


def main() -> int:
    for cmd in STEPS:
        print(f"\n=== orangebook: {cmd} ===", flush=True)
        proc = subprocess.run([sys.executable, str(SCRIPT), cmd], cwd=str(HERE))
        if proc.returncode != 0:
            print(f"orangebook step '{cmd}' failed (exit {proc.returncode})", file=sys.stderr)
            return proc.returncode
    print("orangebook: all steps completed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
