# edsuite CLI Pipeline Reference

This repository hosts a Unix-style pipeline for processing scanned student tests. Each stage reads JSONL from stdin, writes JSONL to stdout, and offers flags for configuration. This guide summarizes the available commands, key flags, and an end-to-end example.

## Stage 1 – OCR (`batchocr/ocr_tests.py`)
- `--input/-i PATH` – PDF file, directory, or `-` for stdin. Supports raw PDF bytes.
- `--output/-o PATH` – JSONL output destination (default stdout).
- `--dpi INT` – Rasterization DPI (default 220).
- `--jpeg-quality INT` – JPEG quality for OCR images (default 70).
- `--language-hints` – Comma-separated Vision hints (`en,es`).
- `--unknown-label` – Prefix when no name is detected (default `Unknown Student`).

## Stage 2 – Cleanup (`cleanocr/cleanup_tests.py`)
- `--input/-i PATH` – JSONL input (default stdin).
- `--output/-o PATH` – JSONL output (default stdout).
- `--max-tokens INT` – Token cap for Grok cleanup (default 1200).
- `--keep-original` – Include original text alongside restored output.

## Stage 3 – Evaluation (`evaluate/evaluate_tests.py`)
- `--input/-i PATH` – JSONL input (default stdin).
- `--output/-o PATH` – JSONL output (default stdout).
- `--material/-file PATH` – Reading material string or text/PDF file.
- `--question/-file PATH` – Essay question string or text/PDF file.
- `--context/-file PATH` – Optional context string or file.
- `--model NAME` – Grok model (default `grok-4-fast-reasoning`).
- `--max-batch-tokens INT` – Token budget per batch (default 12000).
- `--max-batch-size INT` – Essay count per batch (default 5).
- `--timeout FLOAT` – HTTP timeout in seconds (default 180).
- `--usage-metadata` – Embed token usage in output metadata.

## Stage 4 – Reporting (`report/report_results.py`)
- `--input/-i PATH` – JSONL input (default stdin).
- `--output/-o PATH` – JSONL passthrough output (default stdout).
- `--csv PATH` – Write summary CSV (optional).
- `--pdf-dir PATH` – Directory for per-student PDF reports (optional).
- `--grade-max FLOAT` – Maximum total score for letter grades (default 10).
- `--no-pass-through` – Generate artifacts without emitting JSON.

## Stage 5 – Mail Payloads (`mailroom/mailroom_cli.py`)
- `--input/-i PATH` – JSONL input (default stdin).
- `--output/-o PATH` – JSONL output (default stdout).
- `--roster PATH` – CSV with `student-name,email-address` columns.
- `--subject TEXT` – Email subject (default “Your Evaluation from Mr. Cooper's AI Krew”).
- `--greeting TEXT` – Greeting prefix (default “Hello”).
- `--report PATH` – Save summary of ready/missing emails.

## Stage 6 – SMTP Send (`mailroom/send_mail.py`)
- `--input/-i PATH` – JSONL input (default stdin).
- `--output/-o PATH` – JSONL output (default stdout).
- `--dry-run` – Log intended sends without contacting SMTP.
- `--rate-limit FLOAT` – Delay between sends (default 0.5s).
- `--attach-report` – Attach PDF at `metadata.report_pdf` when present.
- `--report PATH` – Save transactional send summary.

## Example End-to-End Pipeline

```bash
python batchocr/ocr_tests.py --input ~/Downloads/students.pdf \
  | python cleanocr/cleanup_tests.py \
  | ./evaluate/evaluate_tests.py --material-file ~/Downloads/material.txt --question-file ~/Downloads/questions.txt --context "10th-grade English" \
  | ./report/report_results.py --csv out/summary.csv --pdf-dir out/reports \
  | ./mailroom/mailroom_cli.py --roster ~/Downloads/mail.csv --report out/mailroom/payload_report.txt \
  | ./mailroom/send_mail.py --attach-report --report out/mailroom/send_report.txt
```

All scripts respect the shared `.env` in the repository root for credentials and defaults. Each stage prints concise start/finish messages to stderr so you can monitor progress while the JSON flows through stdout for further processing.

