#!/usr/bin/env python
"""Generate synthetic OCR output covering single- and multi-page tests."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from batchocr.ocr_tests import PageResult, aggregate_tests


def make_page(
    *,
    number: int,
    body: str,
    detected_name: str | None = None,
    continuation_name: str | None = None,
) -> PageResult:
    return PageResult(
        number=number,
        text=body,
        detected_name=detected_name,
        continuation_name=continuation_name,
    )


def main() -> int:
    sample_pdf = "sample_batch.pdf"
    pages = [
        make_page(
            number=1,
            body="Name: Alice Adams\nEssay body page 1.\n[[UNK]] gaps appear here.",
            detected_name="Alice Adams",
        ),
        make_page(
            number=2,
            body="CONTINUE: Alice Adams\nRemainder of Alice's essay.\n[[UNK]] placeholder.",
            continuation_name="Alice Adams",
        ),
        make_page(
            number=3,
            body="Name: Bob Brown\nSingle-page response.\n[[UNK]] token for cleanup.",
            detected_name="Bob Brown",
        ),
        make_page(
            number=4,
            body="CONTINUE: Cara Chen\nSecond page scanned first.",
            continuation_name="Cara Chen",
        ),
        make_page(
            number=5,
            body="Name: Cara Chen\nFirst page scanned second.",
            detected_name="Cara Chen",
        ),
        make_page(
            number=6,
            body="Name: Dana Diaz\nOne page only.",
            detected_name="Dana Diaz",
        ),
    ]

    aggregates = aggregate_tests(pages)
    output_path = Path("out") / "sample_batch.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for aggregate in aggregates:
            record = aggregate.to_json_record(original_pdf=sample_pdf)
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Wrote {output_path} with {len(aggregates)} records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
