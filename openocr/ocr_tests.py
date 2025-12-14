#!/usr/bin/env python
"""
OCR-only CLI that mirrors batchocr but uses Qwen via OpenRouter instead of Google Vision.

The workflow matches batchocr:
- Reads PDF paths from stdin (one per line) or raw PDF bytes piped via stdin.
- Emits JSON lines (one per detected student test) to stdout or --output.
- Writes diagnostics to stderr.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

import regex
import requests
from pdf2image import convert_from_bytes, convert_from_path

NAME_HEADER_PATTERN = regex.compile(
    r"(?im)^\s*(?:name|id)\s*[:\-]\s*([\p{L}][\p{L}'-]*(?:\s+[\p{L}][\p{L}'-]*)?)"
)
CONTINUE_HEADER_PATTERN = regex.compile(r"(?im)^\s*continue\s*[:\-]\s*(.+)$")

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT_DIR / ".env"

DEFAULT_PROMPT = (
    "Transcribe the handwritten and printed text from this scanned student test page. "
    "Preserve the natural reading order and include headings such as Name/ID, prompts, "
    "and responses exactly as written."
)
DEFAULT_API_URL = "https://openrouter.ai/api/v1/chat/completions"


@dataclass
class PageResult:
    number: int
    text: str
    detected_name: Optional[str]
    continuation_name: Optional[str]


@dataclass
class TestAggregate:
    student_name: str
    start_page: int
    end_page: int
    parts: list[str]

    def append_page(self, text: str, page_number: int) -> None:
        self.parts.append(text)
        if page_number < self.start_page:
            self.start_page = page_number
        if page_number > self.end_page:
            self.end_page = page_number

    def to_json_record(self, original_pdf: str) -> dict:
        return {
            "student_name": self.student_name,
            "text": "\n\n".join(self.parts),
            "metadata": {
                "original_pdf": original_pdf,
                "start_page": self.start_page,
                "end_page": self.end_page,
                "page_count": self.end_page - self.start_page + 1,
            },
        }


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


class QwenClient:
    """Minimal wrapper for sending OCR prompts to OpenRouter Qwen models."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        prompt: str,
        api_url: str,
        max_tokens: int,
        temperature: float,
        request_timeout: float = 120.0,
    ) -> None:
        if not api_key:
            raise RuntimeError("QWEN_API_KEY is not configured.")
        if not model:
            raise RuntimeError("QWEN_API_MODEL is not configured.")
        self._prompt = prompt
        self._model = model
        self._api_url = api_url
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._timeout = request_timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    def document_text(self, image_bytes: bytes) -> str:
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                        },
                    ],
                }
            ],
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }
        response = self._session.post(self._api_url, json=payload, timeout=self._timeout)
        if response.status_code != 200:
            raise RuntimeError(
                f"Qwen API call failed (status={response.status_code}): {response.text}"
            )
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as error:  # noqa: PERF203
            raise RuntimeError(f"Unexpected Qwen response format: {error}") from error
        text = self._normalize_content(content)
        if not text.strip():
            raise RuntimeError("Qwen returned empty transcription.")
        return text

    @staticmethod
    def _normalize_content(content: object) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(part for part in parts if part)
        return ""

    def close(self) -> None:
        self._session.close()


def detect_name(text: str) -> Optional[str]:
    """Detect student name in the top portion of the OCR text."""
    top_section = "\n".join(text.splitlines()[:10])
    match = NAME_HEADER_PATTERN.search(top_section)
    if match:
        return match.group(1).strip()
    return None


def detect_continuation_name(text: str) -> Optional[str]:
    """Detect CONTINUE markers that reference the original student name."""
    top_section = "\n".join(text.splitlines()[:10])
    match = CONTINUE_HEADER_PATTERN.search(top_section)
    if match:
        return match.group(1).strip()
    return None


def convert_pdf_to_images(
    *,
    source_path: Optional[Path],
    pdf_bytes: Optional[bytes],
    dpi: int,
    image_format: str,
    jpeg_quality: int,
) -> list[io.BytesIO]:
    if source_path is None and pdf_bytes is None:
        raise ValueError("PDF source not provided.")

    images = (
        convert_from_path(str(source_path), dpi=dpi)
        if source_path is not None
        else convert_from_bytes(pdf_bytes, dpi=dpi)
    )

    buffers: list[io.BytesIO] = []
    for image in images:
        buffer = io.BytesIO()
        rgb_image = image.convert("RGB")
        rgb_image.save(buffer, format=image_format.upper(), quality=jpeg_quality, optimize=True)
        buffer.seek(0)
        buffers.append(buffer)
    return buffers


def ocr_pdf(
    *,
    source_path: Optional[Path],
    pdf_bytes: Optional[bytes],
    client: QwenClient,
    dpi: int,
    image_format: str,
    jpeg_quality: int,
) -> list[PageResult]:
    buffers = convert_pdf_to_images(
        source_path=source_path,
        pdf_bytes=pdf_bytes,
        dpi=dpi,
        image_format=image_format,
        jpeg_quality=jpeg_quality,
    )

    results: list[PageResult] = []
    for index, buffer in enumerate(buffers, start=1):
        text = client.document_text(buffer.getvalue())
        name = detect_name(text)
        continuation = detect_continuation_name(text)
        results.append(
            PageResult(number=index, text=text, detected_name=name, continuation_name=continuation)
        )
    return results


def aggregate_tests(pages: Iterable[PageResult], *, unknown_prefix: str = "Unknown Student") -> list[TestAggregate]:
    aggregates: list[TestAggregate] = []
    current: Optional[TestAggregate] = None
    unknown_counter = 0
    aggregates_by_name: dict[str, TestAggregate] = {}
    pending_by_name: dict[str, list[PageResult]] = {}

    def normalize_name(name: Optional[str]) -> Optional[str]:
        if not name:
            return None
        collapsed = regex.sub(r"\s+", " ", name).strip()
        if not collapsed:
            return None
        return collapsed.casefold()

    def attach_pending(name_key: Optional[str], aggregate: TestAggregate) -> None:
        if not name_key:
            return
        pending_pages = pending_by_name.pop(name_key, [])
        for pending_page in sorted(pending_pages, key=lambda item: item.number):
            aggregate.append_page(pending_page.text, pending_page.number)

    for page in pages:
        if page.continuation_name:
            continuation_key = normalize_name(page.continuation_name)
            target = aggregates_by_name.get(continuation_key) if continuation_key else None
            if target is not None:
                target.append_page(page.text, page.number)
            else:
                if continuation_key:
                    pending_by_name.setdefault(continuation_key, []).append(page)
                else:
                    unknown_counter += 1
                    aggregate = TestAggregate(
                        student_name=f"{unknown_prefix} {unknown_counter:02d}",
                        start_page=page.number,
                        end_page=page.number,
                        parts=[page.text],
                    )
                    aggregates.append(aggregate)
            continue

        if page.detected_name:
            if current is not None:
                aggregates.append(current)
            current = TestAggregate(
                student_name=page.detected_name,
                start_page=page.number,
                end_page=page.number,
                parts=[page.text],
            )
            name_key = normalize_name(page.detected_name)
            if name_key:
                aggregates_by_name[name_key] = current
                attach_pending(name_key, current)
            continue

        if current is None:
            unknown_counter += 1
            current = TestAggregate(
                student_name=f"{unknown_prefix} {unknown_counter:02d}",
                start_page=page.number,
                end_page=page.number,
                parts=[page.text],
            )
        else:
            current.append_page(page.text, page.number)

    if current is not None:
        aggregates.append(current)

    for pending_key, pending_pages in pending_by_name.items():
        pending_pages.sort(key=lambda item: item.number)
        continuation_label = pending_pages[0].continuation_name
        if not continuation_label:
            unknown_counter += 1
            continuation_label = f"{unknown_prefix} {unknown_counter:02d}"
        aggregate = TestAggregate(
            student_name=continuation_label,
            start_page=pending_pages[0].number,
            end_page=pending_pages[0].number,
            parts=[],
        )
        for pending_page in pending_pages:
            aggregate.append_page(pending_page.text, pending_page.number)
        aggregates.append(aggregate)
    return aggregates


def resolve_input_items(input_arg: str) -> Iterator[tuple[Optional[Path], Optional[bytes]]]:
    if input_arg != "-":
        path = Path(input_arg).expanduser()
        if path.is_dir():
            for pdf_path in sorted(path.glob("*.pdf")):
                yield pdf_path, None
            return
        if not path.exists():
            raise FileNotFoundError(f"Input path not found: {path}")
        yield path, None
        return

    stdin_buffer = sys.stdin.buffer
    peek = stdin_buffer.peek(5)
    if not peek:
        return

    if peek.lstrip().startswith(b"%PDF"):
        data = stdin_buffer.read()
        yield None, data
        return

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        yield Path(line).expanduser(), None


def resolve_output_stream(path: Optional[str]) -> io.TextIOBase:
    if path:
        return open(path, "w", encoding="utf-8")
    return sys.stdout


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OCR batched student tests from scanned PDFs using Qwen and emit JSON lines.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--input",
        "-i",
        default="-",
        help="PDF file, directory, or '-' for stdin (paths or raw PDF bytes).",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Write JSONL results to this file instead of stdout.",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=220,
        help="Rasterization DPI passed to pdf2image.",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=70,
        help="JPEG quality (1-100) when encoding pages.",
    )
    parser.add_argument(
        "--unknown-label",
        default="Unknown Student",
        help="Prefix used when no name header is detected.",
    )
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help="Instruction string prepended before each page image.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=2048,
        help="Max tokens requested from the Qwen API.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.1,
        help="Generation temperature passed to the Qwen API.",
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help="Override the Qwen/OpenRouter API URL.",
    )
    return parser.parse_args(argv)


def load_qwen_credentials() -> tuple[str, str]:
    api_key = os.environ.get("QWEN_API_KEY", "").strip()
    model = os.environ.get("QWEN_API_MODEL", "").strip()
    if not api_key:
        raise RuntimeError("QWEN_API_KEY is missing. Set it in the environment or .env file.")
    if not model:
        raise RuntimeError("QWEN_API_MODEL is missing. Set it in the environment or .env file.")
    return api_key, model


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_env_file(ENV_FILE)
    print("[openocr] Starting OCR processing...", file=sys.stderr)
    args = parse_args(argv)

    try:
        api_key, model = load_qwen_credentials()
        client = QwenClient(
            api_key=api_key,
            model=model,
            prompt=args.prompt,
            api_url=args.api_url,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
        )
    except Exception as error:  # noqa: BLE001
        print(f"Failed to initialize Qwen client: {error}", file=sys.stderr)
        return 1

    exit_code = 0
    output_stream = resolve_output_stream(args.output)
    managed_output = output_stream is not sys.stdout

    try:
        for source_path, pdf_bytes in resolve_input_items(args.input):
            original_label = str(source_path) if source_path is not None else "-"
            try:
                pages = ocr_pdf(
                    source_path=source_path,
                    pdf_bytes=pdf_bytes,
                    client=client,
                    dpi=args.dpi,
                    image_format="jpeg",
                    jpeg_quality=args.jpeg_quality,
                )
            except Exception as error:  # noqa: BLE001
                exit_code = 1
                print(f"OCR failed for {original_label}: {error}", file=sys.stderr)
                continue

            aggregates = aggregate_tests(pages, unknown_prefix=args.unknown_label)
            if not aggregates:
                exit_code = 1
                print(f"No pages processed for {original_label}", file=sys.stderr)
                continue

            for aggregate in aggregates:
                record = aggregate.to_json_record(original_pdf=original_label)
                output_stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    finally:
        client.close()
        if managed_output:
            output_stream.close()

    print(f"[openocr] Completed OCR processing (exit={exit_code}).", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

