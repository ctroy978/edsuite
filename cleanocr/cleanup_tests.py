#!/usr/bin/env python
"""
Cleanup CLI for restoring OCR output using xAI Grok.

This tool sits after the OCR stage in the pipeline:
- Reads JSON lines from stdin (default) or --input file.
- Sends each record's `text` through the cleanup model to fill [[UNK]] gaps.
- Emits updated JSON lines to stdout or --output, preserving metadata and adding cleanup details.

Example:
    python batchocr/ocr_tests.py --input scans.pdf | ./cleanocr/cleanup_tests.py > restored.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional, Sequence

import httpx
import regex


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT_DIR / ".env"

WORD_PATTERN = regex.compile(r"\b[\p{L}\p{N}'-]+\b")
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Guardrail knobs: loosened coverage + expansion limits to trust model output more.
COVERAGE_THRESHOLD = 0.5  # was 0.7
MAX_EXPANSION_FACTOR = 0.5  # allow +50% extra words versus previous 20%
MIN_EXPANSION_BUFFER = 20  # ensure short passages get headroom


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class CleanupResult:
    restored_text: str
    attempts: int
    guardrail_triggered: bool
    guardrail_violated: bool


class CleanupClient:
    """Synchronous HTTP client for xAI cleanup."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout: float = 60.0,
        read_timeout: float = 120.0,
    ) -> None:
        if not api_key:
            raise RuntimeError("XAI_API_KEY is not configured.")
        self._model = model or "grok-4-fast-reasoning"
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") or "https://api.x.ai/v1",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(timeout, read=read_timeout),
        )

    def close(self) -> None:
        self._client.close()

    def restore(
        self,
        masked_text: str,
        *,
        temperature: float,
        max_tokens: int,
        max_attempts: int,
    ) -> str:
        clean_text = regex.sub(r"\[\[UNK]]", " ", masked_text)
        approx_words = len(WORD_PATTERN.findall(clean_text))
        unk_slots = masked_text.count("[[UNK]]")
        baseline_target = approx_words + max(unk_slots, 0)
        allowed_extra = max(5, int(0.2 * max(baseline_target, 1)))
        word_limit = baseline_target + allowed_extra

        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You receive OCR'd student writing with low-confidence spans marked as [[UNK]]. "
                        "Return a fluent restoration that stays faithful to the student's intent. "
                        "Do not add commentary."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Restore the text below. It originally contains about {approx_words} words and "
                        "{unk_slots} placeholder(s). Keep the result within {word_limit} words.\n\n"
                        "<BEGIN_TEXT>\n{masked}\n<END_TEXT>"
                    ).format(
                        masked=masked_text,
                        approx_words=approx_words,
                        unk_slots=unk_slots,
                        word_limit=word_limit,
                    ),
                },
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        attempt = 0
        last_error: Optional[Exception] = None
        while attempt < max_attempts:
            attempt += 1
            try:
                response = self._client.post("/chat/completions", json=payload)
                if response.status_code in RETRYABLE_STATUS:
                    backoff = min(8, 2**attempt)
                    if "Retry-After" in response.headers:
                        try:
                            backoff = float(response.headers["Retry-After"])
                        except ValueError:
                            pass
                    time.sleep(backoff)
                    continue
                response.raise_for_status()
                data = response.json()
                choices = data.get("choices") or []
                if not choices:
                    raise ValueError("xAI response missing choices")
                content = choices[0].get("message", {}).get("content")
                if content is None:
                    raise ValueError("xAI response missing content")
                return content.strip()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt >= max_attempts:
                    raise
                time.sleep(min(8, 2**attempt))
        raise RuntimeError("Failed to obtain cleanup result") from last_error


def guardrail_ok(masked_text: str, restored_text: str) -> bool:
    clean_original = regex.sub(r"\[\[UNK]]", " ", masked_text)
    original_counts: Dict[str, int] = {}
    restored_counts: Dict[str, int] = {}
    for token in WORD_PATTERN.findall(clean_original):
        key = token.lower()
        original_counts[key] = original_counts.get(key, 0) + 1
    for token in WORD_PATTERN.findall(restored_text):
        key = token.lower()
        restored_counts[key] = restored_counts.get(key, 0) + 1

    original_total = sum(original_counts.values())
    if original_total == 0:
        return True

    overlap = sum(min(count, restored_counts.get(word, 0)) for word, count in original_counts.items())
    coverage = overlap / original_total
    if coverage < COVERAGE_THRESHOLD:
        return False

    unk_slots = masked_text.count("[[UNK]]")
    baseline_target = original_total + max(unk_slots, 0)
    restored_total = sum(restored_counts.values())
    allowed_extra = max(MIN_EXPANSION_BUFFER, int(MAX_EXPANSION_FACTOR * max(baseline_target, 1)))
    if restored_total > baseline_target + allowed_extra:
        return False

    return True


def restore_text(masked_text: str, client: CleanupClient, *, max_tokens: int) -> CleanupResult:
    restored_text = masked_text
    guardrail_triggered = False
    guardrail_violated = False
    attempts = 0

    for attempts, temperature in enumerate((0.2, 0.0), start=1):
        restored_text = client.restore(
            masked_text,
            temperature=temperature,
            max_tokens=max_tokens,
            max_attempts=3,
        )
        if guardrail_ok(masked_text, restored_text):
            if attempts > 1:
                guardrail_triggered = True
            return CleanupResult(
                restored_text=restored_text,
                attempts=attempts,
                guardrail_triggered=guardrail_triggered,
                guardrail_violated=False,
            )
        guardrail_triggered = True

    guardrail_violated = not guardrail_ok(masked_text, restored_text)
    return CleanupResult(
        restored_text=restored_text,
        attempts=attempts,
        guardrail_triggered=guardrail_triggered,
        guardrail_violated=guardrail_violated,
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restore OCR text by filling [[UNK]] tokens using xAI Grok.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--input",
        "-i",
        default="-",
        help="JSONL file to read (default: stdin).",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Write JSONL results to this path instead of stdout.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=1200,
        help="max_tokens passed to the cleanup model.",
    )
    parser.add_argument(
        "--keep-original",
        action="store_true",
        help="Retain the original text in output under 'original_text'.",
    )
    return parser.parse_args(argv)


def iter_input_lines(path: str) -> Iterator[str]:
    if path == "-":
        for line in sys.stdin:
            yield line
    else:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                yield line


def resolve_output_stream(path: Optional[str]):
    if path:
        return open(path, "w", encoding="utf-8")
    return sys.stdout


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_env_file(ENV_FILE)
    print("[cleanocr] Starting cleanup...", file=sys.stderr)
    args = parse_args(argv)

    api_key = os.environ.get("XAI_API_KEY")
    model = os.environ.get("XAI_CLEANUP_MODEL", "grok-4-fast-reasoning")
    base_url = os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1")

    try:
        client = CleanupClient(api_key=api_key, model=model, base_url=base_url)
    except Exception as error:  # noqa: BLE001
        print(f"Failed to initialize cleanup client: {error}", file=sys.stderr)
        return 1

    exit_code = 0
    output_stream = resolve_output_stream(args.output)
    managed_output = output_stream is not sys.stdout

    try:
        for raw_line in iter_input_lines(args.input):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                exit_code = 1
                print(f"Invalid JSON input: {error}: {line}", file=sys.stderr)
                continue

            original_text = record.get("text")
            if not isinstance(original_text, str):
                exit_code = 1
                print("Input record missing string 'text' field; skipping.", file=sys.stderr)
                continue

            try:
                result = restore_text(original_text, client, max_tokens=args.max_tokens)
            except Exception as error:  # noqa: BLE001
                exit_code = 1
                identifier = record.get("student_name") or record.get("metadata", {}).get("original_pdf", "-")
                print(f"Cleanup failed for {identifier}: {error}", file=sys.stderr)
                continue

            updated_metadata = dict(record.get("metadata") or {})
            cleanup_meta = {
                "attempts": result.attempts,
                "guardrail_triggered": result.guardrail_triggered,
                "guardrail_violated": result.guardrail_violated,
            }
            updated_metadata["cleanup"] = cleanup_meta

            output_record = dict(record)
            if args.keep_original:
                output_record["original_text"] = original_text
            output_record["text"] = result.restored_text
            output_record["metadata"] = updated_metadata

            output_stream.write(json.dumps(output_record, ensure_ascii=False) + "\n")
    finally:
        client.close()
        if managed_output:
            output_stream.close()

    print(f"[cleanocr] Completed cleanup (exit={exit_code}).", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
