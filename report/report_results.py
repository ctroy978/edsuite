#!/usr/bin/env python
"""Generate CSV/PDF reports and augmented JSONL from evaluator streams."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = ROOT_DIR / ".env"


@dataclass
class NormalizedCriterion:
    name: str
    key: str
    score: Optional[float]
    max_score: Optional[float]
    label: Optional[str]
    explanation: Optional[str]
    evidence: Optional[str]


@dataclass
class NormalizedEvaluation:
    index: int
    schema: str
    student_name: str
    file_name: Optional[str]
    summary: Optional[str]
    criteria: List[NormalizedCriterion] = field(default_factory=list)
    total_score: Optional[float] = None
    max_score: Optional[float] = None
    final_grade: str = "N/A"
    overall_comment: Optional[str] = None
    improvement_advice: Optional[str] = None
    improvement_example: Optional[str] = None
    improvement_improved_example: Optional[str] = None
    report_pdf_path: Optional[str] = None


def iter_input_records(path: str) -> Iterator[Dict[str, Any]]:
    source = sys.stdin if path == "-" else open(path, "r", encoding="utf-8")
    with source:
        for raw_line in source:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                print(f"[report] Skipping invalid JSON: {error}: {line}", file=sys.stderr)
                continue
            if isinstance(record, dict):
                yield record
            else:
                print("[report] Skipping non-object JSON entry", file=sys.stderr)


def detect_schema(record: Dict[str, Any]) -> str:
    if isinstance(record.get("evaluation"), dict):
        return "tests"
    if "rubric_scores" in record or "max_score" in record:
        return "essay"
    return "unknown"


def extract_score(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    if isinstance(value, dict):
        return extract_score(value.get("score"))
    return None


def letter_grade(total_score: Optional[float], max_score: Optional[float]) -> str:
    if total_score is None or max_score is None or max_score <= 0:
        return "N/A"
    ratio = max(0.0, min(total_score / max_score, 1.0))
    if ratio >= 0.9:
        return "A"
    if ratio >= 0.8:
        return "B"
    if ratio >= 0.7:
        return "C"
    if ratio >= 0.6:
        return "D"
    return "F"


def normalize_record(record: Dict[str, Any], schema: str, index: int, grade_max: float) -> NormalizedEvaluation:
    if schema == "tests":
        return normalize_tests_record(record, index, grade_max)
    if schema == "essay":
        return normalize_essay_record(record, index, grade_max)
    return normalize_unknown_record(record, index, grade_max)


def normalize_tests_record(record: Dict[str, Any], index: int, grade_max: float) -> NormalizedEvaluation:
    evaluation = record.get("evaluation")
    if not isinstance(evaluation, dict):
        raise ValueError("Record missing 'evaluation' payload")

    student_name = str(evaluation.get("student_name") or record.get("student_name") or "Unknown Student")
    summary = stringify(evaluation.get("summary") or record.get("summary"))
    overall_comment = stringify(evaluation.get("overall_comment") or record.get("overall_comment"))

    criteria: List[NormalizedCriterion] = []
    for key in sorted(k for k in evaluation.keys() if k.startswith("criterion")):
        payload = evaluation.get(key)
        criteria.append(build_criterion_from_payload(payload, key, default_name=key.replace("_", " ").title()))

    total_score = extract_score(evaluation.get("total_score"))
    if total_score is None and criteria:
        full_scores = [c.score for c in criteria if c.score is not None]
        if len(full_scores) == len(criteria):
            total_score = sum(full_scores)

    max_score = extract_score(evaluation.get("max_score"))
    effective_max = max_score if max_score and max_score > 0 else (grade_max if grade_max > 0 else None)
    final_grade = letter_grade(total_score, effective_max)

    return NormalizedEvaluation(
        index=index,
        schema="tests",
        student_name=student_name,
        file_name=record.get("file_name"),
        summary=summary,
        criteria=criteria,
        total_score=total_score,
        max_score=effective_max,
        final_grade=final_grade,
        overall_comment=overall_comment,
    )


def normalize_essay_record(record: Dict[str, Any], index: int, grade_max: float) -> NormalizedEvaluation:
    student_name = str(record.get("student_name") or record.get("evaluation", {}).get("student_name") or "Unknown Student")
    file_name = stringify(record.get("file_name"))
    summary = stringify(record.get("summary") or record.get("evaluation", {}).get("summary"))
    overall_comment = stringify(record.get("overall_comment"))

    rubric_scores = record.get("rubric_scores")
    criteria: List[NormalizedCriterion] = []
    if isinstance(rubric_scores, list):
        for seq, item in enumerate(rubric_scores):
            if not isinstance(item, dict):
                continue
            name = stringify(item.get("criterion_name")) or f"Criterion {seq + 1}"
            key = slugify(name) or f"criterion_{seq + 1}"
            score = extract_score(item.get("score"))
            max_score = extract_score(item.get("max_score"))
            if max_score is None:
                max_score = 4.0
            criteria.append(
                NormalizedCriterion(
                    name=name,
                    key=key,
                    score=score,
                    max_score=max_score,
                    label=stringify(item.get("label")),
                    explanation=stringify(item.get("explanation")),
                    evidence=stringify(item.get("evidence")),
                )
            )

    total_score = extract_score(record.get("total_score"))
    if total_score is None and criteria:
        total_score = sum(c.score for c in criteria if c.score is not None) if all(c.score is not None for c in criteria) else None
    max_score = extract_score(record.get("max_score"))
    if max_score is None and criteria:
        per_crit_max = sum(c.max_score or 0 for c in criteria)
        max_score = per_crit_max if per_crit_max > 0 else None
    effective_max = max_score if max_score and max_score > 0 else (grade_max if grade_max > 0 else None)
    final_grade = letter_grade(total_score, effective_max)

    improvement = record.get("improvement_tutorial") or {}
    advice = stringify(improvement.get("advice"))
    example = stringify(improvement.get("example_from_essay"))
    improved_example = stringify(improvement.get("improved_example"))

    return NormalizedEvaluation(
        index=index,
        schema="essay",
        student_name=student_name,
        file_name=file_name,
        summary=summary,
        criteria=criteria,
        total_score=total_score,
        max_score=max_score if max_score and max_score > 0 else effective_max,
        final_grade=final_grade,
        overall_comment=overall_comment,
        improvement_advice=advice,
        improvement_example=example,
        improvement_improved_example=improved_example,
    )


def normalize_unknown_record(record: Dict[str, Any], index: int, grade_max: float) -> NormalizedEvaluation:
    evaluation = record.get("evaluation")
    student_name = str(
        record.get("student_name")
        or (evaluation.get("student_name") if isinstance(evaluation, dict) else "")
        or "Unknown Student"
    )
    file_name = stringify(record.get("file_name"))
    summary = stringify(record.get("summary"))
    overall_comment = stringify(record.get("overall_comment"))

    criteria: List[NormalizedCriterion] = []
    if isinstance(record.get("rubric_scores"), list):
        for seq, item in enumerate(record["rubric_scores"]):
            if isinstance(item, dict):
                name = stringify(item.get("criterion_name")) or f"Criterion {seq + 1}"
                criteria.append(
                    NormalizedCriterion(
                        name=name,
                        key=slugify(name) or f"criterion_{seq + 1}",
                        score=extract_score(item.get("score")),
                        max_score=extract_score(item.get("max_score")) or 4.0,
                        label=stringify(item.get("label")),
                        explanation=stringify(item.get("explanation")),
                        evidence=stringify(item.get("evidence")),
                    )
                )
    elif isinstance(evaluation, dict):
        for key in sorted(k for k in evaluation.keys() if k.startswith("criterion")):
            payload = evaluation.get(key)
            criteria.append(build_criterion_from_payload(payload, key, default_name=key.replace("_", " ").title()))

    total_score = extract_score(record.get("total_score"))
    if total_score is None and isinstance(evaluation, dict):
        total_score = extract_score(evaluation.get("total_score"))
    if total_score is None and criteria:
        candidate_scores = [c.score for c in criteria if c.score is not None]
        if len(candidate_scores) == len(criteria):
            total_score = sum(candidate_scores)

    max_score = extract_score(record.get("max_score"))
    if max_score is None and isinstance(evaluation, dict):
        max_score = extract_score(evaluation.get("max_score"))
    if max_score is None and grade_max > 0:
        max_score = grade_max

    effective_max = max_score if max_score and max_score > 0 else (grade_max if grade_max > 0 else None)
    final_grade = letter_grade(total_score, effective_max)

    improvement = record.get("improvement_tutorial") or {}
    advice = stringify(improvement.get("advice"))
    example = stringify(improvement.get("example_from_essay"))
    improved_example = stringify(improvement.get("improved_example"))

    if not summary and isinstance(evaluation, dict):
        summary = stringify(evaluation.get("summary"))
    if not overall_comment and isinstance(evaluation, dict):
        overall_comment = stringify(evaluation.get("overall_comment"))

    return NormalizedEvaluation(
        index=index,
        schema="unknown",
        student_name=student_name,
        file_name=file_name,
        summary=summary,
        criteria=criteria,
        total_score=total_score,
        max_score=effective_max,
        final_grade=final_grade,
        overall_comment=overall_comment,
        improvement_advice=advice,
        improvement_example=example,
        improvement_improved_example=improved_example,
    )


def build_criterion_from_payload(payload: Any, key: str, default_name: str) -> NormalizedCriterion:
    if isinstance(payload, dict):
        name = stringify(payload.get("name")) or default_name
        return NormalizedCriterion(
            name=name,
            key=key,
            score=extract_score(payload),
            max_score=extract_score(payload.get("max_score")),
            label=stringify(payload.get("label")),
            explanation=stringify(payload.get("explanation")),
            evidence=stringify(payload.get("evidence")),
        )
    return NormalizedCriterion(
        name=default_name,
        key=key,
        score=extract_score(payload),
        max_score=None,
        label=None,
        explanation=None,
        evidence=None,
    )


def stringify(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def slugify(value: Optional[str]) -> str:
    if not value:
        return ""
    sanitized = "".join(ch if ch.isalnum() else "-" for ch in value.strip())
    sanitized = "-".join(filter(None, sanitized.split("-")))
    return sanitized.lower()


def ensure_parent_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def generate_csv(evaluations: List[NormalizedEvaluation], csv_path: Path) -> None:
    ensure_parent_directory(csv_path)
    criterion_order: List[str] = []
    seen: set[str] = set()
    for evaluation in evaluations:
        for criterion in evaluation.criteria:
            name = criterion.name or criterion.key
            if not name or name in seen:
                continue
            seen.add(name)
            criterion_order.append(name)

    headers = ["Student Name"]
    for name in criterion_order:
        headers.append(f"{name} Score")
    headers.extend(["Total Score", "Max Score", "Final Grade"])

    with open(csv_path, "w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=headers)
        writer.writeheader()
        for evaluation in evaluations:
            row: Dict[str, Any] = {"Student Name": evaluation.student_name}
            for name in criterion_order:
                row[f"{name} Score"] = format_score_for_cell(find_criterion(evaluation, name))
            row["Total Score"] = format_number(evaluation.total_score)
            row["Max Score"] = format_number(evaluation.max_score)
            row["Final Grade"] = evaluation.final_grade
            writer.writerow(row)


def find_criterion(evaluation: NormalizedEvaluation, name: str) -> Optional[NormalizedCriterion]:
    for criterion in evaluation.criteria:
        if (criterion.name or criterion.key) == name:
            return criterion
    return None


def format_score_for_cell(criterion: Optional[NormalizedCriterion]) -> str:
    if not criterion or criterion.score is None:
        return ""
    if criterion.max_score:
        return f"{criterion.score:g}/{criterion.max_score:g}"
    return f"{criterion.score:g}"


def format_number(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:g}"


def build_pdf_report(target_dir: Path, evaluation: NormalizedEvaluation) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(evaluation.student_name) or f"student-{evaluation.index}"
    pdf_path = target_dir / f"{evaluation.index:04d}-{slug}.pdf"

    c = canvas.Canvas(str(pdf_path), pagesize=letter)
    width, height = letter
    cursor_y = height - inch
    line_height = 14
    wrap_width = 95

    def ensure_space(lines: int = 1) -> None:
        nonlocal cursor_y
        if cursor_y <= inch + lines * line_height:
            c.showPage()
            cursor_y = height - inch

    def write_line(text: str = "", *, bold: bool = False) -> None:
        nonlocal cursor_y
        ensure_space()
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 10)
        c.drawString(inch, cursor_y, text)
        cursor_y -= line_height

    def write_block(title: str, text: Optional[str], indent: int = 0) -> None:
        if not text:
            return
        write_line(title, bold=True)
        write_wrapped(text, indent=indent)
        write_line()

    def write_wrapped(text: str, indent: int = 0) -> None:
        prefix = " " * indent
        paragraphs = text.splitlines() or [""]
        for paragraph in paragraphs:
            if not paragraph.strip():
                write_line()
                continue
            for line in textwrap.wrap(paragraph.strip(), width=wrap_width - indent):
                write_line(f"{prefix}{line}")

    c.setTitle(f"Evaluation Report - {evaluation.student_name}")
    write_line(f"Student: {evaluation.student_name}", bold=True)
    if evaluation.file_name:
        write_line(f"File: {evaluation.file_name}")
    if evaluation.total_score is not None:
        if evaluation.max_score:
            write_line(f"Total Score: {evaluation.total_score:g} / {evaluation.max_score:g}")
        else:
            write_line(f"Total Score: {evaluation.total_score:g}")
    else:
        write_line("Total Score: N/A")
    write_line(f"Final Grade: {evaluation.final_grade}")
    write_line()

    write_block("Summary", evaluation.summary)

    for criterion in evaluation.criteria:
        name = criterion.name or criterion.key
        write_line(name or "Criterion", bold=True)
        score_parts: List[str] = []
        if criterion.score is not None:
            if criterion.max_score:
                score_parts.append(f"Score: {criterion.score:g}/{criterion.max_score:g}")
            else:
                score_parts.append(f"Score: {criterion.score:g}")
        else:
            score_parts.append("Score: N/A")
        if criterion.label:
            score_parts.append(f"Label: {criterion.label}")
        write_line(" | ".join(score_parts))
        if criterion.explanation:
            write_wrapped(f"Explanation: {criterion.explanation}", indent=2)
        if criterion.evidence:
            write_wrapped(f"Evidence: {criterion.evidence}", indent=2)
        write_line()

    write_block("Overall Comment", evaluation.overall_comment)

    if evaluation.improvement_advice or evaluation.improvement_example or evaluation.improvement_improved_example:
        write_line("Improvement Tutorial", bold=True)
        if evaluation.improvement_advice:
            write_wrapped(f"Advice: {evaluation.improvement_advice}", indent=2)
        if evaluation.improvement_example:
            write_wrapped(f"Needs Revision: {evaluation.improvement_example}", indent=2)
        if evaluation.improvement_improved_example:
            write_wrapped(f"Improved Example: {evaluation.improvement_improved_example}", indent=2)
        write_line()

    c.save()
    return pdf_path


def augment_record(record: Dict[str, Any], evaluation: NormalizedEvaluation) -> Dict[str, Any]:
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        record["metadata"] = metadata

    if evaluation.final_grade is not None:
        metadata["final_grade"] = evaluation.final_grade
    if evaluation.total_score is not None:
        metadata["total_score"] = evaluation.total_score
    if evaluation.report_pdf_path:
        metadata["report_pdf"] = evaluation.report_pdf_path

    record["report_summary"] = {
        "student_name": evaluation.student_name,
        "total_score": evaluation.total_score,
        "max_score": evaluation.max_score,
        "final_grade": evaluation.final_grade,
        "criteria": [
            {
                "name": criterion.name,
                "key": criterion.key,
                "score": criterion.score,
                "max_score": criterion.max_score,
                "label": criterion.label,
            }
            for criterion in evaluation.criteria
        ],
    }
    return record


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate CSV/PDF reports and augmented JSONL from evaluator output.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", "-i", default="-", help="JSONL input path or '-' for stdin")
    parser.add_argument("--csv-output", "--csv", "-c", dest="csv_output", default=None, help="Path to write the CSV summary")
    parser.add_argument("--pdf-dir", "-p", dest="pdf_dir", default=None, help="Directory for per-student PDF reports")
    parser.add_argument("--grade-max", type=float, default=10.0, help="Fallback max score for letter grades when missing")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    evaluations: List[NormalizedEvaluation] = []
    pdf_dir = Path(args.pdf_dir) if args.pdf_dir else None
    csv_path = Path(args.csv_output) if args.csv_output else None

    try:
        record_iter = iter_input_records(args.input)
    except FileNotFoundError as error:
        print(f"[report] Input file not found: {error}", file=sys.stderr)
        return 1

    total_records = 0
    processed_records = 0
    try:
        for index, record in enumerate(record_iter):
            total_records += 1
            schema = detect_schema(record)
            try:
                normalized = normalize_record(record, schema, index, args.grade_max)
            except Exception as error:  # noqa: BLE001
                print(f"[report] Failed to normalize record #{index + 1}: {error}", file=sys.stderr)
                continue

            if pdf_dir:
                try:
                    pdf_path = build_pdf_report(pdf_dir, normalized)
                    normalized.report_pdf_path = str(pdf_path)
                except Exception as error:  # noqa: BLE001
                    print(f"[report] Failed to generate PDF for {normalized.student_name}: {error}", file=sys.stderr)

            augment_record(record, normalized)
            print(json.dumps(record, ensure_ascii=False))
            evaluations.append(normalized)
            processed_records += 1
    except Exception as error:  # noqa: BLE001
        print(f"[report] Failed during processing: {error}", file=sys.stderr)
        return 1

    if csv_path and evaluations:
        try:
            generate_csv(evaluations, csv_path)
        except Exception as error:  # noqa: BLE001
            print(f"[report] Failed to write CSV: {error}", file=sys.stderr)
            return 1

    print(f"[report] Processed {processed_records}/{total_records} records.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
