#!/usr/bin/env python
"""
Send transactional emails using Brevo SMTP based on JSONL mail payloads.

Expected input records contain a `mail` object produced by `mailroom_cli.py`.
This script sends messages for entries whose `mail.status` is "ready" and writes
an updated JSON stream downstream so additional tooling can continue to operate.

Example:
    python .../mailroom/mailroom_cli.py --roster roster.csv \\
        | ./mailroom/send_mail.py --attach-report --report out/mailroom/send_report.txt
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
import time
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Dict, Iterable, Iterator, Optional, Sequence


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT_DIR / ".env"

REQUIRED_ENV_VARS = [
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_TLS",
    "SMTP_USER",
    "SMTP_PASS",
    "FROM_EMAIL",
    "FROM_NAME",
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


def parse_bool(value: str) -> bool:
    truthy = {"true", "1", "yes", "on"}
    falsy = {"false", "0", "no", "off"}
    lowered = value.strip().lower()
    if lowered in truthy:
        return True
    if lowered in falsy:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def get_smtp_settings() -> Dict[str, str]:
    missing = [key for key in REQUIRED_ENV_VARS if not os.getenv(key)]
    if missing:
        raise RuntimeError(f"Missing required SMTP environment variables: {', '.join(missing)}")
    settings = {key: os.getenv(key, "").strip() for key in REQUIRED_ENV_VARS}
    settings["SMTP_TLS"] = parse_bool(settings["SMTP_TLS"])
    try:
        settings["SMTP_PORT"] = int(settings["SMTP_PORT"])
    except ValueError as error:  # noqa: BLE001
        raise RuntimeError("SMTP_PORT must be an integer") from error
    return settings


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


def build_message(
    *,
    mail_payload: dict,
    from_name: str,
    from_email: str,
    attachments: Iterable[Path],
) -> MIMEMultipart:
    message = MIMEMultipart()
    message["From"] = formataddr((from_name, from_email))
    message["To"] = mail_payload["to"]
    message["Subject"] = mail_payload.get("subject") or "Your Results"
    message.attach(MIMEText(mail_payload.get("body") or "", "plain"))

    for attachment in attachments:
        if not attachment.exists():
            print(f"Attachment not found: {attachment}", file=sys.stderr)
            continue
        with attachment.open("rb") as handle:
            data = handle.read()
        part = MIMEApplication(data, Name=attachment.name)
        part["Content-Disposition"] = f'attachment; filename="{attachment.name}"'
        message.attach(part)

    return message


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send emails via Brevo SMTP using JSONL mail payloads.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", "-i", default="-", help="JSONL input or '-' for stdin")
    parser.add_argument("--output", "-o", default=None, help="JSONL output (default: stdout)")
    parser.add_argument("--env-file", default=str(ENV_FILE), help="Path to shared .env file with SMTP credentials")
    parser.add_argument("--dry-run", action="store_true", help="Log intended sends without contacting SMTP")
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=0.5,
        help="Seconds to sleep between consecutive sends (default: 0.5)",
    )
    parser.add_argument(
        "--attach-report",
        action="store_true",
        help="Attach the PDF report referenced in metadata.report_pdf when available",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Optional path to write a summary of send outcomes",
    )
    return parser.parse_args(argv)


def resolve_attachments(record: dict, enable: bool) -> list[Path]:
    if not enable:
        return []
    metadata = record.get("metadata") or {}
    report_path = metadata.get("report_pdf")
    if isinstance(report_path, str) and report_path.strip():
        return [Path(report_path)]
    return []


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    load_env_file(Path(args.env_file))
    print("[send_mail] Starting SMTP delivery...", file=sys.stderr)

    try:
        smtp_settings = get_smtp_settings()
    except Exception as error:  # noqa: BLE001
        print(f"SMTP configuration error: {error}", file=sys.stderr)
        return 1

    output_stream = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    managed_output = output_stream is not sys.stdout

    sent_count = 0
    skipped_count = 0
    error_count = 0
    processed = 0

    report_entries: list[str] = []

    server: Optional[smtplib.SMTP] = None

    if not args.dry_run:
        try:
            server = smtplib.SMTP(smtp_settings["SMTP_HOST"], smtp_settings["SMTP_PORT"], timeout=30)
            server.ehlo()
            if smtp_settings["SMTP_TLS"]:
                context = ssl.create_default_context()
                server.starttls(context=context)
                server.ehlo()
            server.login(smtp_settings["SMTP_USER"], smtp_settings["SMTP_PASS"])
        except smtplib.SMTPException as error:  # noqa: BLE001
            print(f"Failed to establish SMTP connection: {error}", file=sys.stderr)
            if server is not None:
                try:
                    server.quit()
                except smtplib.SMTPException:
                    pass
            if managed_output:
                output_stream.close()
            return 1

    try:
        for record in iter_json_records(args.input):
            processed += 1
            mail_payload = record.get("mail")
            if not isinstance(mail_payload, dict):
                skipped_count += 1
                report_entries.append(f"SKIP no_mail_payload\t{record.get('student_name', 'Unknown')}")
                output_stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                continue

            status = mail_payload.get("status")
            recipient = mail_payload.get("to")
            student = record.get("student_name") or mail_payload.get("student_name") or "Unknown"

            if status != "ready" or not recipient:
                skipped_count += 1
                report_entries.append(f"SKIP {status or 'unknown'}\t{student}")
                output_stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                continue

            attachments = resolve_attachments(record, args.attach_report)
            message = build_message(
                mail_payload=mail_payload,
                from_name=smtp_settings["FROM_NAME"],
                from_email=smtp_settings["FROM_EMAIL"],
                attachments=attachments,
            )

            if args.dry_run:
                mail_payload["status"] = "dry_run"
                mail_payload["error"] = None
                sent_count += 1
                report_entries.append(f"DRY_RUN\t{student}\t{recipient}")
            else:
                try:
                    assert server is not None
                    server.sendmail(
                        smtp_settings["FROM_EMAIL"],
                        [recipient],
                        message.as_string(),
                    )
                    mail_payload["status"] = "sent"
                    mail_payload["error"] = None
                    sent_count += 1
                    report_entries.append(f"SENT\t{student}\t{recipient}")
                except smtplib.SMTPException as error:  # noqa: BLE001
                    mail_payload["status"] = "error"
                    mail_payload["error"] = str(error)
                    error_count += 1
                    report_entries.append(f"ERROR\t{student}\t{recipient}\t{error}")
                    print(f"Failed to send to {recipient}: {error}", file=sys.stderr)

            record.setdefault("metadata", {})
            record["metadata"]["mail_status"] = mail_payload.get("status")
            output_stream.write(json.dumps(record, ensure_ascii=False) + "\n")

            if args.rate_limit > 0 and not args.dry_run:
                time.sleep(args.rate_limit)
    finally:
        if server is not None:
            try:
                server.quit()
            except smtplib.SMTPException:
                pass
        if managed_output:
            output_stream.close()

    if args.report:
        try:
            report_path = Path(args.report)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with report_path.open("w", encoding="utf-8") as handle:
                handle.write(f"Processed: {processed}\n")
                handle.write(f"Sent: {sent_count}\n")
                handle.write(f"Skipped: {skipped_count}\n")
                handle.write(f"Errors: {error_count}\n")
                if report_entries:
                    handle.write("\nDetails:\n")
                    for entry in report_entries:
                        handle.write(f"{entry}\n")
        except Exception as error:  # noqa: BLE001
            print(f"Failed to write report file: {error}", file=sys.stderr)

    print(
        f"Processed {processed}. Sent: {sent_count}. Skipped: {skipped_count}. Errors: {error_count}.",
        file=sys.stderr,
    )
    print("[send_mail] SMTP delivery complete.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
