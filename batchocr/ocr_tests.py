#!/usr/bin/env python
"""
OCR-only CLI for scanning multi-test PDF batches.

The tool embraces Unix pipelines:
- Reads PDF paths from stdin (one per line) or raw PDF bytes piped via stdin.
- Writes JSON lines to stdout (one object per student test) unless --output is used.
- Emits human-readable diagnostics to stderr and uses non-zero exit codes on failure.

Example:
    cat batch.pdf | ./ocr_tests.py | python next_tool.py
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

import regex
from google.cloud import vision
from google.oauth2 import service_account
from pdf2image import convert_from_bytes, convert_from_path


NAME_HEADER_PATTERN = regex.compile(
    r"(?im)^\s*(?:name|id)\s*[:\-]\s*([\p{L}][\p{L}'-]*(?:\s+[\p{L}][\p{L}'-]*)?)"
)
CONTINUE_HEADER_PATTERN = regex.compile(r"(?im)^\s*continue\s*[:\-]\s*(.+)$")

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT_DIR / ".env"


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


class VisionClient:
    """Minimal Google Vision wrapper with explicit credential handling."""

    def __init__(self, credentials_path: Path, *, language_hints: Sequence[str]) -> None:
        if not credentials_path.exists():
            raise RuntimeError(f"Google credentials file not found: {credentials_path}")
        credentials = service_account.Credentials.from_service_account_file(str(credentials_path))
        self._client = vision.ImageAnnotatorClient(credentials=credentials)
        self._language_hints = list(language_hints)

    def document_text(self, image_bytes: bytes) -> vision.AnnotateImageResponse:
        image = vision.Image(content=image_bytes)
        image_context = {"language_hints": self._language_hints} if self._language_hints else None
        response = self._client.document_text_detection(image=image, image_context=image_context)
        if response.error.message:
            raise RuntimeError(f"Google Vision error: {response.error.message}")
        return response

    def close(self) -> None:
        transport_close = getattr(self._client.transport, "close", None)
        if callable(transport_close):
            transport_close()


def detect_name(text: str) -> Optional[str]:
    """Detect student name in the top portion of the OCR text."""
    # Limit search to the first ~10 lines to reduce false positives deeper in the page.
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


def extract_text(response: vision.AnnotateImageResponse) -> str:
    annotation = response.full_text_annotation
    if annotation and annotation.text:
        return annotation.text
    if response.text_annotations:
        return response.text_annotations[0].description
    return ""


def convert_pdf_to_images(
    *,
    source_path: Optional[Path],
    pdf_bytes: Optional[bytes],
    dpi: int,
    image_format: str,
    jpeg_quality: int,
) -> list[io.BytesIO]:
    """Convert a PDF into JPEG buffers for Vision upload."""
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
    client: VisionClient,
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
        response = client.document_text(buffer.getvalue())
        text = extract_text(response)
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
        description="OCR batched student tests from scanned PDFs and emit JSON lines.",
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
        help="JPEG quality (1-100) when encoding pages for Vision.",
    )
    parser.add_argument(
        "--language-hints",
        default="",
        help="Comma-separated Vision language hints (e.g. 'en,es').",
    )
    parser.add_argument(
        "--unknown-label",
        default="Unknown Student",
        help="Prefix used when no name header is detected.",
    )
    return parser.parse_args(argv)


def load_credentials_path() -> Path:
    env_value = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not env_value:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS environment variable is not set.")
    return Path(env_value).expanduser()


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_env_file(ENV_FILE)
    print("[batchocr] Starting OCR processing...", file=sys.stderr)
    args = parse_args(argv)
    language_hints = [hint.strip() for hint in args.language_hints.split(",") if hint.strip()]
    credentials_path = load_credentials_path()

    try:
        client = VisionClient(credentials_path, language_hints=language_hints)
    except Exception as error:  # noqa: BLE001
        print(f"Failed to initialize Google Vision client: {error}", file=sys.stderr)
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

    print(f"[batchocr] Completed OCR processing (exit={exit_code}).", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
