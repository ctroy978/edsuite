#!/usr/bin/env python
"""
Essay evaluation CLI that fits the Unix-style pipeline.

Consumes JSONL (typically the output of the cleanup stage) and emits JSONL with
evaluation details added for each essay. Relies on the shared root .env for
configuration and communicates with the xAI Grok API.

Example pipelines:
    python batchocr/ocr_tests.py --input scans.pdf \
        | python cleanocr/cleanup_tests.py \
        | ./evaluate/evaluate_tests.py --material-file material.txt --question-file question.txt

    cat cleaned.jsonl | ./evaluate/evaluate_tests.py \
        --material "Short reading passage" \
        --question "Explain the author's use of fate." \
        --context "10th-grade English" \
        --output results.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import httpx
from pypdf import PdfReader


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT_DIR / ".env"

API_BASE_URL = "https://api.x.ai/v1"
API_PATH = "/chat/completions"
DEFAULT_MODEL = "grok-4-fast-reasoning"
DEFAULT_MAX_BATCH_TOKENS = 12000
DEFAULT_MAX_BATCH_SIZE = 5
TOKEN_ESTIMATE_CHAR_RATIO = 4


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


def read_text_resource(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Resource not found: {path}")
    if path.suffix.lower() == ".pdf":
        return read_pdf_text(path)
    return path.read_text(encoding="utf-8")


def read_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    pages: List[str] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # pragma: no cover - defensive
            raise RuntimeError(f"Failed to extract text from {path} page {page_number}: {exc}") from exc
        pages.append(text.strip())
    combined = "\n\n".join(filter(None, pages)).strip()
    return combined


def estimate_tokens(*texts: str) -> int:
    total_chars = sum(len(t) for t in texts if t)
    return max(1, total_chars // TOKEN_ESTIMATE_CHAR_RATIO)


@dataclass
class Submission:
    index: int
    student_name: str
    text: str
    raw_record: Dict[str, Any]


def build_prompt_header(material: str, question: str, context: str) -> str:
    material = material.strip() or "[No material provided]"
    question = question.strip() or "[No question provided]"
    context = context.strip() or "[No additional context]"
    return (
        "You are an AI evaluator for 10th-grade high school essays on various literature topics. "
        "Evaluate each essay based on the provided reading material, question, and context. "
        "Account for potential OCR artifacts (minor misspellings or garbled words) without inventing new content.\n\n"
        f"Reading Material:\n{material}\n\n"
        f"Test Question:\n{question}\n\n"
        f"Context:\n{context}\n\n"
        "Now evaluate the following essays:\n"
    )


def format_essay_block(seq_num: int, submission: Submission) -> str:
    body = submission.text.strip() or "[No essay text provided]"
    student = submission.student_name or "Unknown Student"
    return f"Essay {seq_num} (Student: {student}):\n{body}\n"


def build_batches(
    submissions: List[Submission],
    prompt_header: str,
    max_tokens: int,
    max_batch_size: int,
) -> List[List[Submission]]:
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    batches: List[List[Submission]] = []
    current: List[Submission] = []
    current_tokens = estimate_tokens(prompt_header)

    for seq_num, submission in enumerate(submissions, start=1):
        block = format_essay_block(seq_num, submission)
        block_tokens = estimate_tokens(block)
        would_exceed_tokens = current and (current_tokens + block_tokens > max_tokens)
        would_exceed_size = len(current) >= max_batch_size
        if current and (would_exceed_tokens or would_exceed_size):
            batches.append(current)
            current = []
            current_tokens = estimate_tokens(prompt_header)

        current.append(submission)
        current_tokens += block_tokens

    if current:
        batches.append(current)
    return batches


class EvaluationClient:
    def __init__(self, api_key: str, *, model: str, timeout: float, base_url: str = API_BASE_URL) -> None:
        if not api_key:
            raise RuntimeError("XAI_API_KEY is required for evaluation")
        self._model = model
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") or API_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(timeout, read=timeout),
        )

    def close(self) -> None:
        self._client.close()

    def evaluate(self, prompt: str) -> Tuple[str, Dict[str, Any]]:
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        }
        response = self._client.post(API_PATH, json=payload)
        response.raise_for_status()
        data = response.json()
        usage = data.get("usage", {}) if isinstance(data, dict) else {}

        content: Optional[str] = None
        choices = data.get("choices") if isinstance(data, dict) else None
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message") or {}
                content = message.get("content")
                if content is None and "text" in first:
                    content = first.get("text")
        if not content:
            raise RuntimeError("No content returned from xAI Grok response")
        return content, usage


def assemble_prompt(header: str, batch: List[Submission]) -> str:
    parts = [header]
    for idx, submission in enumerate(batch, start=1):
        parts.append(format_essay_block(idx, submission))
    parts.append(
        "For each essay, respond with a JSON array entry of the form:\n"
        "{\n"
        '  "student_name": "...",\n'
        '  "summary": "1-2 sentence summary",\n'
        '  "criterion_1": {"explanation": "...", "score": 1-5},\n'
        '  "criterion_2": {"explanation": "...", "score": 1-5},\n'
        '  "total_score": integer sum of criteria,\n'
        '  "overall_comment": "1-2 sentence holistic comment"\n'
        "}\n\nReturn a JSON array covering all essays."
    )
    return "\n".join(parts)


def parse_evaluations(raw_content: str) -> List[Dict[str, Any]]:
    try:
        parsed = json.loads(raw_content)
        if isinstance(parsed, dict):
            return [parsed]
        if not isinstance(parsed, list):
            raise ValueError
        return parsed
    except Exception:
        trimmed = extract_json_array(raw_content)
        if not trimmed:
            raise
        parsed = json.loads(trimmed)
        if isinstance(parsed, dict):
            return [parsed]
        if not isinstance(parsed, list):
            raise ValueError("Expected JSON array in evaluation output")
        return parsed


def extract_json_array(raw_text: str) -> Optional[str]:
    start = raw_text.find("[")
    end = raw_text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    return raw_text[start : end + 1]


def align_batch(batch: List[Submission], evaluations: List[Dict[str, Any]]) -> List[Tuple[Submission, Optional[Dict[str, Any]]]]:
    aligned: List[Tuple[Submission, Optional[Dict[str, Any]]]] = []
    used_indices: set[int] = set()
    for submission in batch:
        match = match_evaluation(submission, evaluations, used_indices)
        aligned.append((submission, match))
    return aligned


def match_evaluation(
    submission: Submission,
    evaluations: List[Dict[str, Any]],
    used_indices: set[int],
) -> Optional[Dict[str, Any]]:
    target_name = (submission.student_name or "").strip().lower()
    for idx, item in enumerate(evaluations):
        if idx in used_indices:
            continue
        candidate = str(item.get("student_name", "")).strip().lower()
        if candidate and candidate == target_name:
            used_indices.add(idx)
            return item
    for idx, item in enumerate(evaluations):
        if idx in used_indices:
            continue
        used_indices.add(idx)
        return item
    return None


def iter_input_records(path: str) -> Iterator[Dict[str, Any]]:
    if path == "-":
        source = sys.stdin
    else:
        source = open(path, "r", encoding="utf-8")
    with source:
        for raw_line in source:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                print(f"Invalid JSON line skipped: {error}: {line}", file=sys.stderr)
                continue
            if not isinstance(record, dict):
                print(f"Non-object JSON skipped: {line}", file=sys.stderr)
                continue
            yield record


def prepare_submissions(records: Iterable[Dict[str, Any]]) -> List[Submission]:
    submissions: List[Submission] = []
    for idx, record in enumerate(records):
        text = record.get("text")
        if not isinstance(text, str) or not text.strip():
            print("Skipping record without textual 'text' field", file=sys.stderr)
            continue
        student_name = record.get("student_name")
        if not isinstance(student_name, str) or not student_name.strip():
            metadata = record.get("metadata") or {}
            student_name = metadata.get("student_name") or "Unknown Student"
        submissions.append(
            Submission(
                index=idx,
                student_name=student_name.strip(),
                text=text,
                raw_record=record,
            )
        )
    return submissions


def resolve_resource(args_value: Optional[str], file_value: Optional[str], *, allow_empty: bool = False) -> str:
    if args_value:
        return args_value
    if file_value:
        return read_text_resource(Path(file_value))
    if allow_empty:
        return ""
    raise ValueError("Required resource missing (provide inline value or file path)")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate cleaned essays via xAI Grok and emit JSONL results.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", "-i", default="-", help="JSONL input file or '-' for stdin")
    parser.add_argument("--output", "-o", default=None, help="Write JSONL results to this file")
    parser.add_argument("--material", default=None, help="Reading material as a raw string")
    parser.add_argument("--material-file", dest="material_file", default=None, help="Path to reading material text/PDF")
    parser.add_argument("--question", default=None, help="Essay question as a raw string")
    parser.add_argument("--question-file", dest="question_file", default=None, help="Path to essay question text/PDF")
    parser.add_argument("--context", default="", help="Additional context string for the evaluator")
    parser.add_argument("--context-file", dest="context_file", default=None, help="Path to context text/PDF")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="xAI Grok model name")
    parser.add_argument("--max-batch-tokens", type=int, default=DEFAULT_MAX_BATCH_TOKENS, help="Approximate token budget per batch")
    parser.add_argument("--max-batch-size", type=int, default=DEFAULT_MAX_BATCH_SIZE, help="Maximum number of essays per batch")
    parser.add_argument("--timeout", type=float, default=180.0, help="HTTP timeout (seconds)")
    parser.add_argument("--env-file", default=str(ENV_FILE), help="Path to .env file with API credentials")
    parser.add_argument("--usage-metadata", action="store_true", help="Attach token usage per batch to output metadata")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    load_env_file(Path(args.env_file))

    try:
        material = resolve_resource(args.material, args.material_file)
        question = resolve_resource(args.question, args.question_file)
        context = resolve_resource(args.context, args.context_file, allow_empty=True)
    except Exception as error:  # noqa: BLE001
        print(f"Failed to load resources: {error}", file=sys.stderr)
        return 1

    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        print("Missing XAI_API_KEY in environment or .env", file=sys.stderr)
        return 1

    submissions = prepare_submissions(iter_input_records(args.input))
    if not submissions:
        print("No valid submissions to evaluate", file=sys.stderr)
        return 1

    prompt_header = build_prompt_header(material, question, context)
    batches = build_batches(submissions, prompt_header, args.max_batch_tokens, args.max_batch_size)

    output_stream = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    managed_output = output_stream is not sys.stdout

    api_base_url = os.environ.get("XAI_BASE_URL", API_BASE_URL)
    client = EvaluationClient(api_key, model=args.model, timeout=args.timeout, base_url=api_base_url)
    exit_code = 0
    aggregated_usage: Dict[str, float] = {}

    try:
        for batch_index, batch in enumerate(batches):
            prompt = assemble_prompt(prompt_header, batch)
            try:
                raw_content, usage = client.evaluate(prompt)
            except Exception as error:  # noqa: BLE001
                exit_code = 1
                print(f"Batch {batch_index + 1} evaluation failed: {error}", file=sys.stderr)
                continue

            try:
                evaluations = parse_evaluations(raw_content)
            except Exception as error:  # noqa: BLE001
                exit_code = 1
                print(f"Batch {batch_index + 1} JSON parse failed: {error}", file=sys.stderr)
                continue

            aligned = align_batch(batch, evaluations)
            for submission, evaluation in aligned:
                record = dict(submission.raw_record)
                evaluation_payload = evaluation or {
                    "student_name": submission.student_name,
                    "summary": "",
                    "criterion_1": {},
                    "criterion_2": {},
                    "total_score": None,
                    "overall_comment": "",
                }
                record["evaluation"] = evaluation_payload
                if args.usage_metadata and usage:
                    record.setdefault("metadata", {})
                    record["metadata"]["evaluation_usage"] = usage
                output_stream.write(json.dumps(record, ensure_ascii=False) + "\n")

            for key, value in usage.items():
                if isinstance(value, (int, float)):
                    aggregated_usage[key] = aggregated_usage.get(key, 0.0) + float(value)
    finally:
        client.close()
        if managed_output:
            output_stream.close()

    if aggregated_usage:
        parts = []
        for key, value in aggregated_usage.items():
            if float(value).is_integer():
                parts.append(f"{key}={int(value)}")
            else:
                parts.append(f"{key}={value:.2f}")
        usage_str = ", ".join(parts)
        print(f"Aggregated token usage: {usage_str}", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
