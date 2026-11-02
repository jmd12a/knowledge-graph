"""Reproducible local checks for evaluation programs, without business/storage/model execution."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.run import check  # noqa: E402


if __name__ == "__main__":
    for source in [*ROOT.joinpath("evals").glob("*.py"), Path(__file__)]:
        compile(source.read_text("utf-8"), str(source), "exec", dont_inherit=True)
    print(json.dumps(check(), ensure_ascii=False, indent=2))
