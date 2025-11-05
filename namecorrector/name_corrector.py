#!/usr/bin/env python
"""
Interactively correct misrecognized student names in JSONL streams.

This command-line tool slots between the mailroom matching step and the email
sender. It reads JSONL records, highlights any students whose email address is
missing, and lets a teacher adjust the name (and optionally email) before the
records continue downstream.

Example usage:
    # Review a saved batch.
    ./mailroom/mailroom_cli.py --roster roster.csv --output unmatched.jsonl
    ./namecorrector/name_corrector.py --input unmatched.jsonl --output corrected.jsonl

    # Inline in a pipeline (reads from stdin, prompts via /dev/tty).
    ./mailroom/mailroom_cli.py --roster roster.csv \
        | ./namecorrector/name_corrector.py --roster-file roster.csv \
        | ./mailroom/send_mail.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, TextIO, Tuple

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - fallback for missing dependency
    load_dotenv = None  # type: ignore[assignment]


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = ROOT_DIR / ".env"
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class RosterEntry:
    name: str
    email: str


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactively correct student names for unmatched records.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", "-i", default="-", help="JSONL file to read or '-' for stdin")
    parser.add_argument("--output", "-o", default=None, help="Write JSONL output to this file (default: stdout)")
    parser.add_argument(
        "--roster-file",
        default=None,
        help="Optional CSV or JSON file with name/email pairs for re-matching",
    )
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_FILE),
        help="Path to the shared .env file for optional secrets",
    )
    return parser.parse_args(argv)


def load_environment(env_path: Path) -> None:
    if not env_path.exists():
        return
    if load_dotenv is None:
        print(
            "python-dotenv is not available; continuing without loading environment variables.",
            file=sys.stderr,
        )
        return
    load_dotenv(env_path)  # type: ignore[arg-type]


def read_jsonl_records(path: str) -> List[dict]:
    source: TextIO
    close_source = False
    if path == "-":
        source = sys.stdin
    else:
        source = open(path, "r", encoding="utf-8")
        close_source = True

    records: List[dict] = []
    try:
        for line_number, raw_line in enumerate(source, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                print(f"Skipping invalid JSON on line {line_number}: {error}", file=sys.stderr)
                continue
            if isinstance(record, dict):
                records.append(record)
            else:
                print(f"Skipping non-object JSON on line {line_number}", file=sys.stderr)
    finally:
        if close_source:
            source.close()
    return records


def normalize_name(name: str) -> str:
    return " ".join(name.strip().split()).lower()


def ensure_metadata(record: dict) -> dict:
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        record["metadata"] = metadata
    return metadata


def apply_email(
    record: dict,
    metadata: dict,
    nc_meta: dict,
    email: str,
    *,
    source: str,
) -> None:
    record["email"] = email
    mail_block = record.get("mail")
    if not isinstance(mail_block, dict):
        mail_block = {}
        record["mail"] = mail_block
    mail_block["to"] = email
    mail_block["status"] = "ready"
    metadata["mail_status"] = "ready"
    nc_meta["email_source"] = source
    if source == "autofill":
        nc_meta["email_autofilled"] = True
    elif source == "manual":
        nc_meta["email_manual"] = True


def suggest_roster_names(student_name: str, roster: Dict[str, RosterEntry], *, limit: int = 3) -> List[str]:
    if not student_name or not student_name.strip():
        return []
    normalized = normalize_name(student_name)
    candidates = list(roster.keys())
    matches = get_close_matches(normalized, candidates, n=limit, cutoff=0.6)
    suggestions: List[str] = []
    for key in matches:
        entry = roster.get(key)
        if entry:
            suggestions.append(entry.name)
    return suggestions


def load_roster(roster_path: Path) -> Dict[str, RosterEntry]:
    if not roster_path.exists():
        raise FileNotFoundError(f"Roster file not found: {roster_path}")

    suffix = roster_path.suffix.lower()
    if suffix == ".csv":
        return load_roster_csv(roster_path)
    if suffix in {".json", ".jsonl"}:
        return load_roster_json(roster_path)

    raise ValueError("Unsupported roster format. Use CSV, JSON, or JSONL.")


def load_roster_csv(roster_path: Path) -> Dict[str, RosterEntry]:
    with roster_path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("Roster CSV must have headers.")
        field_map = {name.strip().lower(): name for name in reader.fieldnames}

        name_key = (
            field_map.get("student-name")
            or field_map.get("student_name")
            or field_map.get("name")
        )
        email_key = (
            field_map.get("email-address")
            or field_map.get("email")
        )
        if not name_key or not email_key:
            raise ValueError("Roster CSV must include name and email columns.")

        roster: Dict[str, RosterEntry] = {}
        for row in reader:
            student_name = str(row.get(name_key, "")).strip()
            email = str(row.get(email_key, "")).strip()
            if student_name and email:
                roster[normalize_name(student_name)] = RosterEntry(student_name, email)
        return roster


def load_roster_json(roster_path: Path) -> Dict[str, RosterEntry]:
    # Supports JSON array or JSONL of objects.
    content = roster_path.read_text(encoding="utf-8").strip()
    roster: Dict[str, RosterEntry] = {}
    if not content:
        return roster
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # Treat as JSONL
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            _ingest_roster_entry(entry, roster)
        return roster

    if isinstance(data, list):
        for entry in data:
            _ingest_roster_entry(entry, roster)
    elif isinstance(data, dict):
        _ingest_roster_entry(data, roster)
    return roster


def _ingest_roster_entry(entry: object, roster: Dict[str, RosterEntry]) -> None:
    if not isinstance(entry, dict):
        return
    name = (
        entry.get("student_name")
        or entry.get("student-name")
        or entry.get("name")
    )
    email = entry.get("email") or entry.get("email-address")
    if isinstance(name, str) and isinstance(email, str) and name.strip() and email.strip():
        roster[normalize_name(name)] = RosterEntry(name.strip(), email.strip())


def extract_current_email(record: dict) -> Optional[str]:
    email = record.get("email")
    if isinstance(email, str) and email.strip():
        return email.strip()

    mail = record.get("mail")
    if isinstance(mail, dict):
        mail_to = mail.get("to")
        if isinstance(mail_to, str) and mail_to.strip():
            return mail_to.strip()

    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        meta_email = metadata.get("email")
        if isinstance(meta_email, str) and meta_email.strip():
            return meta_email.strip()
    return None


def extract_mail_status(record: dict) -> Optional[str]:
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        status = metadata.get("mail_status")
        if isinstance(status, str):
            return status
    mail = record.get("mail")
    if isinstance(mail, dict):
        status = mail.get("status")
        if isinstance(status, str):
            return status
    return None


def needs_review(record: dict) -> bool:
    status = extract_mail_status(record)
    email_present = extract_current_email(record) is not None

    if status in {"no_email", "invalid_email", "failed"}:
        return True

    if email_present:
        return False

    return True


def open_tty() -> Optional[TextIO]:
    if sys.stdin.isatty():
        return sys.stdin
    try:
        return open("/dev/tty", "r")
    except OSError:
        return None


def prompt(tty: Optional[TextIO], message: str) -> str:
    print(message, file=sys.stderr, end="", flush=True)
    if tty is None:
        return ""
    try:
        response = tty.readline()
    except OSError:
        return ""
    if not response:
        return ""
    return response.rstrip("\n")


def summarize_context(record: dict, *, max_chars: int = 160) -> str:
    candidate_fields = [
        record.get("report"),
        record.get("text"),
        record.get("evaluation", {}).get("summary") if isinstance(record.get("evaluation"), dict) else None,
    ]
    for field in candidate_fields:
        if isinstance(field, str) and field.strip():
            snippet = " ".join(field.strip().split())
            if len(snippet) > max_chars:
                return snippet[: max_chars - 3] + "..."
            return snippet
    return ""


@dataclass
class CorrectionStats:
    reviewed: int = 0
    name_updates: int = 0
    email_updates: int = 0


def interactive_corrections(records: List[dict], roster: Optional[Dict[str, RosterEntry]]) -> CorrectionStats:
    stats = CorrectionStats()
    tty = open_tty()
    if tty is None:
        print(
            "Warning: No interactive terminal available. Records will pass through unchanged.",
            file=sys.stderr,
        )
        return stats

    try:
        for index, record in enumerate(records, start=1):
            if not needs_review(record):
                continue

            stats.reviewed += 1
            original_name = record.get("student_name") or "Unknown Student"
            context = summarize_context(record)

            print("\n--- Review Record #{index} ---".format(index=index), file=sys.stderr)
            print(f"Current name : {original_name}", file=sys.stderr)
            if context:
                print(f"Context      : {context}", file=sys.stderr)
            metadata = ensure_metadata(record)
            nc_meta = metadata.setdefault("name_corrector", {})
            mail_status = extract_mail_status(record)
            if mail_status:
                print(f"Mail status  : {mail_status}", file=sys.stderr)

            if roster:
                suggestions = suggest_roster_names(original_name, roster, limit=3)
                if suggestions:
                    print(
                        "Suggestions  : {names}".format(names=", ".join(suggestions)),
                        file=sys.stderr,
                    )

            corrected_name = prompt(
                tty,
                f"Enter corrected name for [{original_name}] (press Enter to keep): ",
            ).strip()
            if corrected_name:
                record["student_name"] = corrected_name
                nc_meta["name_corrected"] = True
                stats.name_updates += 1
            else:
                corrected_name = original_name
                nc_meta["name_corrected"] = nc_meta.get("name_corrected", False)

            if roster:
                normalized = normalize_name(corrected_name)
                roster_entry = roster.get(normalized)
                if roster_entry:
                    record["student_name"] = roster_entry.name
                    apply_email(record, metadata, nc_meta, roster_entry.email, source="autofill")
                    stats.email_updates += 1
                    print(f"Matched email: {roster_entry.email}", file=sys.stderr)

            if extract_current_email(record) is None:
                manual_email = prompt(
                    tty,
                    f"Enter email for [{corrected_name}] (optional, Enter to skip): ",
                ).strip()
                if manual_email:
                    if EMAIL_REGEX.match(manual_email):
                        apply_email(record, metadata, nc_meta, manual_email, source="manual")
                        stats.email_updates += 1
                    else:
                        print("Invalid email entered. Leaving email unset.", file=sys.stderr)

            nc_meta["reviewed"] = True
    finally:
        if tty is not sys.stdin and tty is not None:
            tty.close()
    return stats


def write_jsonl(records: Iterable[dict], output_path: Optional[str]) -> None:
    destination: TextIO
    close_destination = False
    if output_path in (None, "-"):
        destination = sys.stdout
    else:
        destination = open(output_path, "w", encoding="utf-8")
        close_destination = True

    try:
        for record in records:
            json.dump(record, destination, ensure_ascii=False)
            destination.write("\n")
            destination.flush()
    finally:
        if close_destination:
            destination.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    load_environment(Path(args.env_file))

    roster: Optional[Dict[str, RosterEntry]] = None
    if args.roster_file:
        try:
            roster = load_roster(Path(args.roster_file))
        except Exception as error:
            print(f"Failed to load roster: {error}", file=sys.stderr)
            return 1

    records = read_jsonl_records(args.input)
    stats = interactive_corrections(records, roster)

    if stats.reviewed:
        print(
            f"Reviewed {stats.reviewed} unmatched records "
            f"(name updates: {stats.name_updates}, email updates: {stats.email_updates}).",
            file=sys.stderr,
        )
    else:
        print("No unmatched records required review.", file=sys.stderr)

    try:
        write_jsonl(records, args.output)
    except OSError as error:
        print(f"Failed to write output: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
