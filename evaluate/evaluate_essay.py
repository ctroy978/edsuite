#!/usr/bin/env python
"""
Essay evaluation CLI for raw PDF submissions using an imported rubric.

Unlike evaluate_tests.py (which consumes cleaned JSONL records), this CLI ingests
PDF or ZIP inputs directly, extracts essay text, applies the WR121-style rubric,
and emits evaluation JSONL plus an optional PDF report.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import textwrap
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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

OUTPUT_INSTRUCTIONS = """Respond with a single JSON array. Each element must have this exact structure:
{
  "student_name": "Use EXACTLY the student name provided for this essay (derived from the filename)",
  "file_name": "original filename",
  "summary": "1-3 sentence description of the thesis/topic.",
  "rubric_scores": [
    {
      "criterion_name": "Criterion title",
      "score": 4,
      "label": "Excellent",
      "explanation": "Why this score fits the rubric.",
      "evidence": "Direct quotation that supports the score."
    }
  ],
  "total_score": 20,
  "max_score": 20,
  "overall_comment": "Holistic feedback.",
  "improvement_tutorial": {
    "advice": "Actionable steps tied to the rubric (<=3 sentences).",
    "example_from_essay": "Single direct quote from the essay that needs revision.",
    "improved_example": "Brief rewritten version of that quote showing the improvement."
  }
}
Rules:
- Never infer or extract a student name from essay text; always echo the provided student identifier.
- Evaluate each criterion independently on a 1-4 scale (1=Poor, 4=Excellent).
- Use ONLY the rubric definitions; do not invent content not found in the essay.
- Every rubric_score entry must include an evidence quote from the essay text.
- Provide total_score as the sum of criterion scores and max_score as the rubric maximum.
- In the improvement_tutorial, clearly distinguish the original excerpt (example_from_essay) from the improved rewrite (improved_example) and keep the rewrite concise.
- Return an array covering every essay, in the same order provided.
"""


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
class RubricCriterionLevel:
    score: int
    label: str
    description: str


@dataclass
class RubricCriterion:
    name: str
    description: str
    levels: List[RubricCriterionLevel]


@dataclass
class Rubric:
    course: str
    description: str
    scale: Dict[int, str]
    criteria: List[RubricCriterion]
    total_points: int


@dataclass
class Submission:
    index: int
    student_name: str
    file_name: str
    text: str
    source_path: Path


def load_rubric(path: Path) -> Rubric:
    if not path.exists():
        raise FileNotFoundError(f"Rubric file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Rubric JSON must be an object")
    rubric_data = data.get("rubric")
    if not isinstance(rubric_data, dict):
        raise ValueError("Rubric JSON missing 'rubric' object")

    course = str(rubric_data.get("course") or "").strip()
    description = str(rubric_data.get("description") or "").strip()
    if not course or not description:
        raise ValueError("Rubric must include 'course' and 'description'")

    scale_data = rubric_data.get("scale")
    if not isinstance(scale_data, dict) or not scale_data:
        raise ValueError("Rubric must include a non-empty 'scale' mapping")
    scale: Dict[int, str] = {}
    for key, value in scale_data.items():
        try:
            score_key = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid score key in scale: {key}") from exc
        scale[score_key] = str(value or "").strip() or f"Score {score_key}"

    criteria_data = rubric_data.get("criteria")
    if not isinstance(criteria_data, list) or not criteria_data:
        raise ValueError("Rubric criteria must be a non-empty list")
    if not 4 <= len(criteria_data) <= 6:
        raise ValueError("Rubric must define between 4 and 6 criteria")

    criteria: List[RubricCriterion] = []
    for idx, criterion in enumerate(criteria_data, start=1):
        if not isinstance(criterion, dict):
            raise ValueError(f"Criterion #{idx} is not an object")
        name = str(criterion.get("name") or "").strip()
        desc = str(criterion.get("description") or "").strip()
        if not name or not desc:
            raise ValueError(f"Criterion #{idx} missing name or description")
        levels_data = criterion.get("levels")
        if not isinstance(levels_data, list) or not levels_data:
            raise ValueError(f"Criterion '{name}' must include levels")
        levels: List[RubricCriterionLevel] = []
        for level in levels_data:
            if not isinstance(level, dict):
                raise ValueError(f"Criterion '{name}' has invalid level entry")
            try:
                score = int(level.get("score"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Criterion '{name}' level missing integer score") from exc
            label = str(level.get("label") or "").strip() or scale.get(score, f"Score {score}")
            level_desc = str(level.get("description") or "").strip()
            if score < 1 or score > 4:
                raise ValueError(f"Criterion '{name}' level score {score} out of 1-4 range")
            if not level_desc:
                raise ValueError(f"Criterion '{name}' level {score} missing description")
            levels.append(RubricCriterionLevel(score=score, label=label, description=level_desc))
        criteria.append(RubricCriterion(name=name, description=desc, levels=levels))

    total_points = rubric_data.get("total_points")
    if isinstance(total_points, int) and total_points > 0:
        max_points = total_points
    else:
        max_points = len(criteria) * 4

    return Rubric(
        course=course,
        description=description,
        scale=scale,
        criteria=criteria,
        total_points=max_points,
    )


def collect_pdf_paths(input_path: Path) -> Tuple[List[Path], Optional[tempfile.TemporaryDirectory]]:
    if not input_path.exists():
        raise FileNotFoundError(f"Input path not found: {input_path}")
    if input_path.is_dir():
        pdfs = sorted(
            p for p in input_path.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"
        )
        return pdfs, None
    if input_path.is_file():
        suffix = input_path.suffix.lower()
        if suffix == ".zip":
            return extract_zip_pdfs(input_path)
        if suffix == ".pdf":
            return [input_path], None
    raise ValueError("Input path must be a directory with PDFs, a .zip, or a .pdf file")


def extract_zip_pdfs(zip_path: Path) -> Tuple[List[Path], tempfile.TemporaryDirectory]:
    temp_dir = tempfile.TemporaryDirectory()
    extracted: List[Path] = []
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            for member in archive.infolist():
                filename = Path(member.filename)
                if member.is_dir() or filename.suffix.lower() != ".pdf":
                    continue
                safe_name = filename.name or f"essay_{len(extracted) + 1}.pdf"
                destination = Path(temp_dir.name) / safe_name
                counter = 1
                while destination.exists():
                    destination = Path(temp_dir.name) / f"{filename.stem}_{counter}{filename.suffix or '.pdf'}"
                    counter += 1
                with archive.open(member, "r") as source, open(destination, "wb") as target:
                    shutil.copyfileobj(source, target)
                extracted.append(destination)
    except Exception:
        temp_dir.cleanup()
        raise
    return sorted(extracted), temp_dir


def guess_student_name(path: Path, text: str) -> str:
    """Derive the student name purely from the filename (e.g., 'first last.pdf')."""

    del text  # filenames now define the student identity

    base_name = path.stem.replace("_", " ").replace("-", " ").strip()
    parts = [part for part in base_name.split() if part]
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}"
    if parts:
        return parts[0]
    return "Unknown Student"


def build_submissions(pdf_paths: List[Path]) -> Tuple[List[Submission], bool]:
    submissions: List[Submission] = []
    had_errors = False
    for idx, pdf_path in enumerate(pdf_paths):
        try:
            text = read_pdf_text(pdf_path)
        except Exception as error:  # noqa: BLE001
            had_errors = True
            print(f"Failed to read {pdf_path}: {error}", file=sys.stderr)
            continue
        student_name = guess_student_name(pdf_path, text)
        submissions.append(
            Submission(
                index=idx,
                student_name=student_name,
                file_name=pdf_path.name,
                text=text.strip(),
                source_path=pdf_path,
            )
        )
    return submissions, had_errors


def build_prompt_header(rubric: Rubric) -> str:
    lines = [
        "You are an AI evaluator for college-level composition (WR 121).",
        "Assess each essay strictly according to the rubric below.",
        "Treat OCR imperfections as minor typos and never invent content absent from the essay.",
        "All scores must be integers between 1 (Poor) and 4 (Excellent).",
        f"Course: {rubric.course}",
        f"Description: {rubric.description}",
        "Scale:",
    ]
    for score in sorted(rubric.scale.keys(), reverse=True):
        lines.append(f"  {score}: {rubric.scale[score]}")
    lines.append("Criteria and performance levels:")
    for criterion in rubric.criteria:
        lines.append(f"- {criterion.name}: {criterion.description}")
        for level in sorted(criterion.levels, key=lambda entry: entry.score, reverse=True):
            lines.append(f"    Score {level.score} ({level.label}): {level.description}")
    lines.append("")
    lines.append(
        "For every essay, quote evidence for each criterion, keep summaries concise (<=3 sentences), "
        "and provide a tutorial with an original excerpt plus a concise improved rewrite."
    )
    return "\n".join(lines)


def format_essay_block(seq_num: int, submission: Submission) -> str:
    body = submission.text.strip() or "[No essay text extracted]"
    student = submission.student_name or "Unknown Student"
    return f"Essay {seq_num} (Student: {student}, File: {submission.file_name}):\n{body}\n"


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
    current_tokens = estimate_tokens(prompt_header + OUTPUT_INSTRUCTIONS)

    for seq_num, submission in enumerate(submissions, start=1):
        block = format_essay_block(seq_num, submission)
        block_tokens = estimate_tokens(block)
        would_exceed_tokens = current and (current_tokens + block_tokens > max_tokens)
        would_exceed_size = len(current) >= max_batch_size
        if current and (would_exceed_tokens or would_exceed_size):
            batches.append(current)
            current = []
            current_tokens = estimate_tokens(prompt_header + OUTPUT_INSTRUCTIONS)
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
    parts = [header, "Essays to evaluate:\n"]
    for idx, submission in enumerate(batch, start=1):
        parts.append(format_essay_block(idx, submission))
    parts.append(OUTPUT_INSTRUCTIONS)
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
    target_file = submission.file_name.strip().lower()
    for idx, item in enumerate(evaluations):
        if idx in used_indices:
            continue
        candidate = str(item.get("student_name", "")).strip().lower()
        candidate_file = str(item.get("file_name", "")).strip().lower()
        if (candidate and candidate == target_name) or (candidate_file and candidate_file == target_file):
            used_indices.add(idx)
            return item
    for idx, item in enumerate(evaluations):
        if idx in used_indices:
            continue
        used_indices.add(idx)
        return item
    return None


def normalize_rubric_scores(
    rubric: Rubric,
    evaluation_scores: Any,
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    score_map: Dict[str, Dict[str, Any]] = {}
    if isinstance(evaluation_scores, list):
        for raw in evaluation_scores:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("criterion_name") or raw.get("name") or "").strip()
            if name:
                score_map[name.lower()] = raw
    for criterion in rubric.criteria:
        raw_entry = score_map.get(criterion.name.lower())
        score_value: Optional[int] = None
        explanation = ""
        evidence = ""
        label = ""
        if raw_entry:
            try:
                score_value = int(raw_entry.get("score"))
            except (TypeError, ValueError):
                score_value = None
            label = str(raw_entry.get("label") or "").strip()
            explanation = str(raw_entry.get("explanation") or "").strip()
            evidence = str(raw_entry.get("evidence") or "").strip()
        if score_value is not None and (score_value < 1 or score_value > 4):
            score_value = None
        if not label and score_value is not None:
            label = rubric.scale.get(score_value, f"Score {score_value}")
        normalized.append(
            {
                "criterion_name": criterion.name,
                "score": score_value,
                "label": label,
                "explanation": explanation,
                "evidence": evidence,
            }
        )
    return normalized


def build_result_record(
    submission: Submission,
    evaluation: Optional[Dict[str, Any]],
    rubric: Rubric,
) -> Dict[str, Any]:
    evaluation = evaluation or {}
    student_name = submission.student_name
    summary = str(evaluation.get("summary") or "").strip()
    normalized_scores = normalize_rubric_scores(rubric, evaluation.get("rubric_scores"))
    total_score = evaluation.get("total_score")
    if not isinstance(total_score, int):
        total_score = sum(score["score"] for score in normalized_scores if isinstance(score.get("score"), int))
    max_score = evaluation.get("max_score")
    if not isinstance(max_score, int):
        max_score = rubric.total_points
    overall_comment = str(evaluation.get("overall_comment") or "").strip()
    tutorial = evaluation.get("improvement_tutorial")
    advice = ""
    example = ""
    improved_example = ""
    if isinstance(tutorial, dict):
        advice = str(tutorial.get("advice") or "").strip()
        example = str(tutorial.get("example_from_essay") or "").strip()
        improved_example = str(tutorial.get("improved_example") or "").strip()

    record = {
        "student_name": student_name,
        "file_name": submission.file_name,
        "summary": summary,
        "rubric_scores": normalized_scores,
        "total_score": total_score if total_score is not None else None,
        "max_score": max_score,
        "overall_comment": overall_comment,
        "improvement_tutorial": {
            "advice": advice,
            "example_from_essay": example,
            "improved_example": improved_example,
        },
        "metadata": {
            "rubric_course": rubric.course,
            "rubric_description": rubric.description,
            "rubric_total_points": rubric.total_points,
        },
    }
    return record


def generate_pdf_report(results: List[Dict[str, Any]], rubric: Rubric, output_path: Path) -> None:
    try:
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.units import inch
        from reportlab.pdfgen import canvas
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("ReportLab is required for --pdf-report. Install via 'pip install reportlab'.") from exc

    width, height = LETTER
    pdf_canvas = canvas.Canvas(str(output_path), pagesize=LETTER)
    text_obj = pdf_canvas.beginText(1 * inch, height - 1 * inch)
    wrap_width = 90

    def ensure_space(lines_needed: int = 1) -> None:
        nonlocal text_obj
        if text_obj.getY() <= 0.75 * inch + (lines_needed * 12):
            pdf_canvas.drawText(text_obj)
            pdf_canvas.showPage()
            text_obj = pdf_canvas.beginText(1 * inch, height - 1 * inch)

    def write_lines(text: str, indent: int = 0) -> None:
        nonlocal text_obj
        indent_str = " " * indent
        for paragraph in text.splitlines() or [""]:
            wrapped = textwrap.wrap(paragraph, width=wrap_width - indent) or [paragraph]
            for line in wrapped:
                ensure_space()
                text_obj.textLine(f"{indent_str}{line}")
            ensure_space()
            text_obj.textLine("")

    write_lines("Essay Evaluation Report", 0)
    write_lines(f"Course: {rubric.course}", 0)
    write_lines(f"Description: {rubric.description}", 0)
    write_lines("")

    for result in results:
        write_lines(f"Student: {result.get('student_name', 'Unknown')} ({result.get('file_name', '')})")
        write_lines(f"Summary: {result.get('summary', '')}", 2)
        write_lines("Rubric Scores:", 0)
        for entry in result.get("rubric_scores", []):
            criterion_name = entry.get("criterion_name", "Criterion")
            score = entry.get("score")
            label = entry.get("label") or ""
            expl = entry.get("explanation") or ""
            evidence = entry.get("evidence") or ""
            write_lines(f"{criterion_name}: {score} ({label})", 4)
            if expl:
                write_lines(f"Explanation: {expl}", 6)
            if evidence:
                write_lines(f"Evidence: {evidence}", 6)
        write_lines(
            f"Total Score: {result.get('total_score')} / {result.get('max_score', rubric.total_points)}",
            2,
        )
        write_lines(f"Overall Comment: {result.get('overall_comment', '')}", 2)
        tutorial = result.get("improvement_tutorial") or {}
        advice = tutorial.get("advice") or ""
        example = tutorial.get("example_from_essay") or ""
        improved_example = tutorial.get("improved_example") or ""
        if advice or example or improved_example:
            write_lines("Improvement Tutorial:", 2)
            if advice:
                write_lines(f"Advice: {advice}", 4)
            if example:
                write_lines(f"Needs Revision: {example}", 4)
            if improved_example:
                write_lines(f"Improved Example: {improved_example}", 4)
        write_lines("-" * 40)

    pdf_canvas.drawText(text_obj)
    pdf_canvas.showPage()
    pdf_canvas.save()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate essay PDFs via xAI Grok using an imported rubric.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input-path", "-i", required=True, help="Directory of PDFs or ZIP file containing PDFs")
    parser.add_argument("--rubric-file", required=True, help="Path to rubric JSON (e.g., WR121_Rubric.json)")
    parser.add_argument("--output", "-o", default=None, help="Optional JSONL output path (default stdout)")
    parser.add_argument("--pdf-report", default=None, help="Optional path to save a human-readable PDF summary")
    parser.add_argument("--model", default=None, help="xAI Grok model name")
    parser.add_argument("--max-batch-tokens", type=int, default=DEFAULT_MAX_BATCH_TOKENS, help="Approximate token budget per batch")
    parser.add_argument("--max-batch-size", type=int, default=DEFAULT_MAX_BATCH_SIZE, help="Maximum essays per batch")
    parser.add_argument("--timeout", type=float, default=180.0, help="HTTP timeout (seconds)")
    parser.add_argument("--env-file", default=str(ENV_FILE), help="Path to .env file with API credentials")
    parser.add_argument("--usage-metadata", action="store_true", help="Attach token usage metadata to each output line")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    print("[evaluate_essay] Starting evaluation...", file=sys.stderr)
    load_env_file(Path(args.env_file))

    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        print("Missing XAI_API_KEY in environment or .env", file=sys.stderr)
        return 1

    try:
        rubric = load_rubric(Path(args.rubric_file))
    except Exception as error:  # noqa: BLE001
        print(f"Failed to load rubric: {error}", file=sys.stderr)
        return 1

    temp_dir: Optional[tempfile.TemporaryDirectory] = None
    submissions: List[Submission] = []
    had_read_errors = False
    try:
        pdf_paths, temp_dir = collect_pdf_paths(Path(args.input_path))
        if not pdf_paths:
            print("No PDF files found in the provided input.", file=sys.stderr)
            return 1
        submissions, had_read_errors = build_submissions(pdf_paths)
        if not submissions:
            print("No readable PDF submissions were found.", file=sys.stderr)
            return 1
    finally:
        if temp_dir:
            temp_dir.cleanup()
    if had_read_errors:
        print("Some PDFs failed to process and were skipped.", file=sys.stderr)

    prompt_header = build_prompt_header(rubric)
    batches = build_batches(submissions, prompt_header, args.max_batch_tokens, args.max_batch_size)

    output_stream = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    managed_output = output_stream is not sys.stdout

    api_base_url = os.environ.get("XAI_BASE_URL", API_BASE_URL)
    model_name = args.model or os.environ.get("XAI_EVALUATE_MODEL") or DEFAULT_MODEL
    client = EvaluationClient(api_key, model=model_name, timeout=args.timeout, base_url=api_base_url)
    exit_code = 1 if had_read_errors else 0
    aggregated_usage: Dict[str, float] = {}
    results: List[Dict[str, Any]] = []

    def emit_placeholders(batch_submissions: List[Submission], usage: Dict[str, Any]) -> None:
        for submission in batch_submissions:
            record = build_result_record(submission, None, rubric)
            if args.usage_metadata and usage:
                record["metadata"]["evaluation_usage"] = usage
            results.append(record)
            output_stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    try:
        for batch_index, batch in enumerate(batches):
            prompt = assemble_prompt(prompt_header, batch)
            try:
                raw_content, usage = client.evaluate(prompt)
            except Exception as error:  # noqa: BLE001
                exit_code = 1
                usage = {}
                print(f"Batch {batch_index + 1} evaluation failed: {error}", file=sys.stderr)
                emit_placeholders(batch, usage)
                continue

            for key, value in usage.items():
                if isinstance(value, (int, float)):
                    aggregated_usage[key] = aggregated_usage.get(key, 0.0) + float(value)

            try:
                evaluations = parse_evaluations(raw_content)
            except Exception as error:  # noqa: BLE001
                exit_code = 1
                print(f"Batch {batch_index + 1} JSON parse failed: {error}", file=sys.stderr)
                emit_placeholders(batch, usage)
                continue

            aligned = align_batch(batch, evaluations)
            for submission, evaluation in aligned:
                record = build_result_record(submission, evaluation, rubric)
                if args.usage_metadata and usage:
                    record["metadata"]["evaluation_usage"] = usage
                results.append(record)
                output_stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    finally:
        client.close()
        if managed_output:
            output_stream.close()

    if args.pdf_report:
        try:
            generate_pdf_report(results, rubric, Path(args.pdf_report))
        except Exception as error:  # noqa: BLE001
            exit_code = 1
            print(f"Failed to generate PDF report: {error}", file=sys.stderr)

    if aggregated_usage:
        parts = []
        for key, value in aggregated_usage.items():
            if float(value).is_integer():
                parts.append(f"{key}={int(value)}")
            else:
                parts.append(f"{key}={value:.2f}")
        usage_str = ", ".join(parts)
        print(f"Aggregated token usage: {usage_str}", file=sys.stderr)
    print(f"[evaluate_essay] Completed evaluation (exit={exit_code}).", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
