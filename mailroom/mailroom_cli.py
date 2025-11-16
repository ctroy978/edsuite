#!/usr/bin/env python
"""
Generate student email payloads from evaluation JSONL streams.

The script reads evaluation records (typically the output from report_results.py),
looks up each student's email address from a roster CSV, and emits the original
JSON augmented with a mail payload ready for downstream delivery.

Usage examples:
    python .../report/report_results.py ... | \
        ./mailroom/mailroom_cli.py --roster roster.csv --output emails.jsonl

    cat evaluated.jsonl | ./mailroom/mailroom_cli.py --roster roster.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional, Sequence


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT_DIR / ".env"
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DEFAULT_SUBJECT = "Your Evaluation from Mr. Cooper's AI Krew"


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


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create email payloads for students from evaluation JSONL input.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", "-i", default="-", help="JSONL file or '-' for stdin")
    parser.add_argument("--output", "-o", default=None, help="Write JSONL output to this file (default: stdout)")
    parser.add_argument("--roster", required=True, help="CSV file containing student-name,email-address columns")
    parser.add_argument(
        "--subject",
        default=DEFAULT_SUBJECT,
        help="Subject line for generated emails",
    )
    parser.add_argument(
        "--env-file",
        default=str(ENV_FILE),
        help="Path to the shared .env file for optional configuration",
    )
    parser.add_argument(
        "--greeting",
        default="Hello",
        help="Greeting prefix before the student's first name",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Optional path to write a summary of email readiness",
    )
    return parser.parse_args(argv)


def iter_json_records(path: str) -> Iterator[dict]:
    source = sys.stdin if path == "-" else open(path, "r", encoding="utf-8")
    with source:
        for raw_line in source:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                print(f"Skipping invalid JSON line: {error}: {line}", file=sys.stderr)
                continue
            if isinstance(record, dict):
                yield record
            else:
                print("Skipping non-object JSON entry", file=sys.stderr)


def normalize_name(name: str) -> str:
    return " ".join(name.strip().split()).lower()


def load_roster(csv_path: Path) -> Dict[str, str]:
    if not csv_path.exists():
        raise FileNotFoundError(f"Roster CSV not found: {csv_path}")
    with csv_path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        normalized_fieldnames = [name.strip().lower() for name in reader.fieldnames or []]
        if {"student-name", "email-address"} - set(normalized_fieldnames):
            raise ValueError("Roster CSV must include 'student-name' and 'email-address' columns.")

        columns = {name.strip().lower(): name for name in reader.fieldnames or []}
        name_key = columns["student-name"]
        email_key = columns["email-address"]

        roster: Dict[str, str] = {}
        for row in reader:
            student_name = str(row.get(name_key, "")).strip()
            email = str(row.get(email_key, "")).strip()
            if not student_name or not email:
                continue
            roster[normalize_name(student_name)] = email

        if not roster:
            raise ValueError("Roster CSV did not yield any student/email pairs.")
        return roster


def validate_email(email: str) -> bool:
    return bool(EMAIL_REGEX.match(email))


def first_name(student_name: str) -> str:
    parts = student_name.strip().split()
    return parts[0] if parts else student_name.strip() or "Student"


def detect_schema(record: dict) -> str:
    if isinstance(record.get("evaluation"), dict):
        return "tests"
    if "rubric_scores" in record or "report_summary" in record:
        return "essay"
    return "unknown"


def compose_email_body_tests(
    *,
    student_name: str,
    greeting: str,
    evaluation: dict,
) -> str:
    summary = evaluation.get("summary") or "No summary provided."
    criterion_1 = evaluation.get("criterion_1") or {}
    criterion_2 = evaluation.get("criterion_2") or {}
    overall_comment = evaluation.get("overall_comment") or "No overall comment provided."

    crit1_score = extract_score(criterion_1)
    crit2_score = extract_score(criterion_2)

    lines = [
        f"{greeting} {first_name(student_name)},",
        "",
        "Mr. Cooper's AI Krew has reviewed your work. Here is your evaluation:",
        "",
        "Summary:",
        summary,
        "",
        "Criterion 1:",
        format_criterion(criterion_1, crit1_score),
        "",
        "Criterion 2:",
        format_criterion(criterion_2, crit2_score),
        "",
        "Overall Comment:",
        overall_comment,
        "",
        "If you have any questions, please talk with your teacher.",
        "",
        "Best,",
        "Mr. Cooper's AI Krew",
    ]
    return "\n".join(lines)


def compose_email_body_essay(
    *,
    student_name: str,
    greeting: str,
    record: dict,
) -> str:
    summary = record.get("summary") or "No summary provided."
    report_summary = record.get("report_summary") if isinstance(record.get("report_summary"), dict) else {}
    total_score = report_summary.get("total_score")
    max_score = report_summary.get("max_score")
    final_grade = report_summary.get("final_grade")
    overall_comment = record.get("overall_comment") or "No overall comment provided."
    rubric_scores = record.get("rubric_scores") if isinstance(record.get("rubric_scores"), list) else []
    improvement = record.get("improvement_tutorial") if isinstance(record.get("improvement_tutorial"), dict) else {}

    overall_line = None
    if total_score is not None or max_score is not None or final_grade:
        score_text = []
        if total_score is not None:
            if max_score:
                score_text.append(f"{total_score:g} / {max_score:g}")
            else:
                score_text.append(f"{total_score:g}")
        elif max_score:
            score_text.append(f"Max Score: {max_score:g}")
        if final_grade:
            if score_text:
                score_text.append(f"({final_grade})")
            else:
                score_text.append(f"Grade: {final_grade}")
        overall_line = "Overall Score: " + " ".join(score_text)

    lines = [
        f"{greeting} {first_name(student_name)},",
        "",
        "Mr. Cooper's AI Krew has reviewed your work. Here is your evaluation:",
        "",
        "Summary:",
        summary,
    ]
    if overall_line:
        lines.extend(["", overall_line])
    lines.extend(["", "Rubric Feedback:"])

    if rubric_scores:
        for entry in rubric_scores:
            if not isinstance(entry, dict):
                continue
            name = entry.get("criterion_name") or entry.get("name") or "Criterion"
            score = entry.get("score")
            label = entry.get("label")
            explanation = entry.get("explanation") or "No explanation provided."
            max_per = entry.get("max_score") or 4
            lines.extend(
                [
                    "",
                    f"Criterion: {name}",
                ]
            )
            if score is not None:
                if max_per:
                    lines.append(f"Score: {score} / {max_per}{f' ({label})' if label else ''}")
                else:
                    lines.append(f"Score: {score}{f' ({label})' if label else ''}")
            elif label:
                lines.append(f"Rating: {label}")
            lines.append(f"Explanation: {explanation}")
    else:
        lines.extend(
            [
                "",
                "No rubric criteria were provided in this evaluation.",
            ]
        )

    lines.extend(
        [
            "",
            "Overall Comment:",
            overall_comment,
        ]
    )

    advice = improvement.get("advice")
    example = improvement.get("example_from_essay")
    improved_example = improvement.get("improved_example")
    if advice or example or improved_example:
        lines.extend(
            [
                "",
                "How to Improve:",
            ]
        )
        if advice:
            lines.append(f"Advice: {advice}")
        if example:
            lines.append(f"From your essay: {example}")
        if improved_example:
            lines.append(f"Improved example: {improved_example}")

    lines.extend(
        [
            "",
            "If you have any questions, please talk with your teacher.",
            "",
            "Best,",
            "Mr. Cooper's AI Krew",
        ]
    )
    return "\n".join(lines)


def extract_score(value: object) -> Optional[float]:
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


def format_criterion(criterion: dict, score: Optional[float]) -> str:
    explanation = ""
    if isinstance(criterion, dict):
        explanation = criterion.get("explanation") or ""
        explanation = explanation.strip()
    parts: list[str] = []
    if score is not None:
        parts.append(f"Score: {score:.1f}")
    if explanation:
        parts.append(f"Explanation: {explanation}")
    return "\n".join(parts) if parts else "No details provided."


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    load_env_file(Path(args.env_file))
    print("[mailroom] Preparing email payloads...", file=sys.stderr)

    try:
        roster = load_roster(Path(args.roster))
    except Exception as error:  # noqa: BLE001
        print(f"Failed to load roster: {error}", file=sys.stderr)
        return 1

    output_stream = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    managed_output = output_stream is not sys.stdout
    processed = 0
    emails_ready = 0
    missing_emails: list[str] = []
    invalid_emails: list[str] = []

    try:
        for record in iter_json_records(args.input):
            processed += 1
            student_name = record.get("student_name") or record.get("evaluation", {}).get("student_name")
            if not isinstance(student_name, str) or not student_name.strip():
                print("Skipping record without student_name.", file=sys.stderr)
                continue

            roster_key = normalize_name(student_name)
            email = roster.get(roster_key)
            status: str
            if email is None:
                status = "no_email"
                print(f"No email found for {student_name}.", file=sys.stderr)
                missing_emails.append(student_name)
            elif not validate_email(email):
                status = "invalid_email"
                print(f"Invalid email address for {student_name}: {email}", file=sys.stderr)
                email = None
                invalid_emails.append(student_name)
            else:
                status = "ready"
                emails_ready += 1

            schema = detect_schema(record)
            if schema == "tests":
                evaluation = record.get("evaluation") or {}
                body = compose_email_body_tests(
                    student_name=student_name,
                    greeting=args.greeting,
                    evaluation=evaluation,
                )
            elif schema == "essay":
                body = compose_email_body_essay(
                    student_name=student_name,
                    greeting=args.greeting,
                    record=record,
                )
            else:
                print(f"Skipping {student_name}: unsupported evaluation schema.", file=sys.stderr)
                continue

            mail_payload = {
                "to": email,
                "subject": args.subject,
                "body": body,
                "status": status,
            }

            record.setdefault("metadata", {})
            record["metadata"]["mail_status"] = status
            record["mail"] = mail_payload

            output_stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    finally:
        if managed_output:
            output_stream.close()

    if args.report:
        try:
            report_path = Path(args.report)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with report_path.open("w", encoding="utf-8") as report_file:
                report_file.write(f"Processed records: {processed}\n")
                report_file.write(f"Emails ready: {emails_ready}\n")
                report_file.write(f"Missing emails: {len(missing_emails)}\n")
                for name in missing_emails:
                    report_file.write(f"  - {name}\n")
                report_file.write(f"Invalid emails: {len(invalid_emails)}\n")
                for name in invalid_emails:
                    report_file.write(f"  - {name}\n")
        except Exception as error:  # noqa: BLE001
            print(f"Failed to write report file: {error}", file=sys.stderr)

    print(f"Processed {processed} records. Emails ready: {emails_ready}.", file=sys.stderr)
    print("[mailroom] Email payload preparation complete.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
