#!/usr/bin/env python
"""Generate teacher-friendly artifacts from evaluation JSONL streams.

Reads evaluation records (typically from the evaluator stage), writes a CSV of
scores and per-student PDF reports, and passes the JSON stream downstream.

Example usage:
    python .../evaluate/evaluate_tests.py ... | \
        ./report/report_results.py --csv out/summary.csv --pdf-dir out/reports
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT_DIR / ".env"

CSV_HEADERS = [
    "Student Name",
    "Criterion 1 Score",
    "Criterion 2 Score",
    "Total Score",
    "Final Grade",
]


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
                print(f"Skipping invalid JSON: {error}: {line}", file=sys.stderr)
                continue
            if isinstance(record, dict):
                yield record
            else:
                print("Skipping non-object JSON entry", file=sys.stderr)


def extract_score(value: Any) -> Optional[float]:
    if isinstance(value, dict):
        score = value.get("score")
        if isinstance(score, (int, float)):
            return float(score)
        if isinstance(score, str):
            try:
                return float(score)
            except ValueError:
                return None
    elif isinstance(value, (int, float)):
        return float(value)
    elif isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def compute_total_score(evaluation: Dict[str, Any]) -> Optional[float]:
    total = evaluation.get("total_score")
    score = extract_score(total)
    if score is not None:
        return score
    crit1 = extract_score(evaluation.get("criterion_1"))
    crit2 = extract_score(evaluation.get("criterion_2"))
    if crit1 is None or crit2 is None:
        return None
    return crit1 + crit2


def letter_grade(total_score: Optional[float], max_score: float = 10.0) -> str:
    if total_score is None or max_score <= 0:
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


def ensure_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def ensure_pdf_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


@dataclass
class PdfReport:
    canvas: canvas.Canvas
    cursor_y: float


def build_pdf(pdf_path: Path, record: Dict[str, Any], evaluation: Dict[str, Any], total_score: Optional[float], final_grade: str) -> None:
    ensure_pdf_directory(pdf_path.parent)
    c = canvas.Canvas(str(pdf_path), pagesize=letter)
    width, height = letter
    left_margin = inch
    top_margin = height - inch
    line_height = 12

    def write_line(text: str = "", *, bold: bool = False) -> None:
        nonlocal top_margin
        if top_margin <= inch:
            c.showPage()
            top_margin = height - inch
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 10)
        c.drawString(left_margin, top_margin, text)
        top_margin -= line_height

    def write_block(title: str, body: str) -> None:
        write_line(title, bold=True)
        for line in wrap_text(body, max_chars=90):
            write_line(line)
        write_line()

    student_name = evaluation.get("student_name") or record.get("student_name") or "Unknown Student"
    c.setTitle(f"Evaluation Report - {student_name}")
    write_line(f"Student: {student_name}", bold=True)
    write_line(f"Final Grade: {final_grade}")
    if total_score is not None:
        write_line(f"Total Score: {total_score:.1f}")
    write_line()

    summary = evaluation.get("summary") or "No summary provided."
    write_block("Summary", summary)

    criterion_1 = evaluation.get("criterion_1") or {}
    crit1_desc = format_criterion("Criterion 1", criterion_1)
    write_block("Criterion 1", crit1_desc)

    criterion_2 = evaluation.get("criterion_2") or {}
    crit2_desc = format_criterion("Criterion 2", criterion_2)
    write_block("Criterion 2", crit2_desc)

    overall_comment = evaluation.get("overall_comment") or "No overall comment provided."
    write_block("Overall Comment", overall_comment)

    original_text = record.get("text") or ""
    if original_text:
        write_block("Essay Text (excerpt)", abbreviate_text(original_text, max_chars=1500))

    c.save()


def wrap_text(text: str, *, max_chars: int) -> List[str]:
    words = text.split()
    lines: List[str] = []
    current: List[str] = []
    length = 0
    for word in words:
        word_len = len(word) + (1 if current else 0)
        if current and length + word_len > max_chars:
            lines.append(" ".join(current))
            current = [word]
            length = len(word)
        else:
            current.append(word)
            length += word_len
    if current:
        lines.append(" ".join(current))
    if not lines:
        return [""]
    return lines


def abbreviate_text(text: str, *, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def format_criterion(name: str, payload: Any) -> str:
    if isinstance(payload, dict):
        explanation = payload.get("explanation") or "No explanation provided."
        score = extract_score(payload)
        score_str = f"Score: {score:.1f}" if score is not None else "Score: N/A"
        return f"{score_str}\n{explanation}"
    score = extract_score(payload)
    if score is not None:
        return f"Score: {score:.1f}"
    return "No details provided."


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate CSV and PDF reports from evaluation JSONL while passing the stream forward.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", "-i", default="-", help="JSONL input or '-' for stdin")
    parser.add_argument("--output", "-o", default=None, help="JSONL output (default: stdout)")
    parser.add_argument("--csv", dest="csv_path", default=None, help="Path to write the summary CSV")
    parser.add_argument("--pdf-dir", dest="pdf_dir", default=None, help="Directory for per-student PDF reports")
    parser.add_argument("--env-file", default=str(ENV_FILE), help="Shared .env file to load")
    parser.add_argument("--grade-max", type=float, default=10.0, help="Maximum total score before mapping to a letter grade")
    parser.add_argument("--no-pass-through", action="store_true", help="Do not emit the JSON stream downstream")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    load_env_file(Path(args.env_file))
    print("[report] Starting report generation...", file=sys.stderr)

    csv_writer = None
    csv_file = None
    if args.csv_path:
        csv_path = Path(args.csv_path)
        ensure_directory(csv_path)
        csv_file = open(csv_path, "w", encoding="utf-8", newline="")
        csv_writer = csv.DictWriter(csv_file, fieldnames=CSV_HEADERS)
        csv_writer.writeheader()

    output_stream = open(args.output, "w", encoding="utf-8") if (args.output and not args.no_pass_through) else sys.stdout
    managed_output = output_stream not in {sys.stdout, sys.stderr}

    total_scores: List[float] = []
    total_count = 0

    try:
        for record in iter_input_records(args.input):
            evaluation = record.get("evaluation")
            if not isinstance(evaluation, dict):
                print("Skipping record without evaluation payload", file=sys.stderr)
                continue

            total_count += 1
            crit1 = extract_score(evaluation.get("criterion_1"))
            crit2 = extract_score(evaluation.get("criterion_2"))
            total_score = compute_total_score(evaluation)
            if total_score is not None:
                total_scores.append(total_score)
            grade = letter_grade(total_score, max_score=args.grade_max)

            if csv_writer:
                csv_writer.writerow(
                    {
                        "Student Name": evaluation.get("student_name") or record.get("student_name") or "Unknown Student",
                        "Criterion 1 Score": f"{crit1:.1f}" if crit1 is not None else "",
                        "Criterion 2 Score": f"{crit2:.1f}" if crit2 is not None else "",
                        "Total Score": f"{total_score:.1f}" if total_score is not None else "",
                        "Final Grade": grade,
                    }
                )

            if args.pdf_dir:
                pdf_dir = Path(args.pdf_dir)
                ensure_pdf_directory(pdf_dir)
                student_slug = slugify(evaluation.get("student_name") or record.get("student_name") or "student")
                pdf_path = pdf_dir / f"{student_slug}.pdf"
                try:
                    build_pdf(pdf_path, record, evaluation, total_score, grade)
                except Exception as error:  # noqa: BLE001
                    print(f"Failed to write PDF for {student_slug}: {error}", file=sys.stderr)
                else:
                    record.setdefault("metadata", {})
                    record["metadata"]["report_pdf"] = str(pdf_path)

            record.setdefault("metadata", {})
            record["metadata"]["final_grade"] = grade
            if total_score is not None:
                record["metadata"]["total_score"] = total_score

            if not args.no_pass_through:
                output_stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    finally:
        if csv_file:
            csv_file.close()
        if managed_output:
            output_stream.close()

    if total_count:
        avg = sum(total_scores) / len(total_scores) if total_scores else 0.0
        print(
            f"Processed {total_count} evaluations. Average total score: {avg:.2f}",
            file=sys.stderr,
        )
    else:
        print("No evaluations processed.", file=sys.stderr)

    print("[report] Report generation complete.", file=sys.stderr)
    return 0


def slugify(value: str) -> str:
    sanitized = "".join(ch if ch.isalnum() else "-" for ch in value.strip())
    sanitized = "-".join(filter(None, sanitized.split("-")))
    return sanitized.lower() or "student"


if __name__ == "__main__":
    sys.exit(main())
