# edsuite CLI Pipeline Reference

This repository hosts a Unix-style pipeline for processing scanned student tests. Each stage reads JSONL from stdin, writes JSONL to stdout, and offers flags for configuration. This guide summarizes the available commands, key flags, and an end-to-end example.

## Stage 1 – OCR (`batchocr/ocr_tests.py`)
- `--input/-i PATH` – PDF file, directory, or `-` for stdin. Supports raw PDF bytes.
- `--output/-o PATH` – JSONL output destination (default stdout).
- `--dpi INT` – Rasterization DPI (default 220).
- `--jpeg-quality INT` – JPEG quality for OCR images (default 70).
- `--language-hints` – Comma-separated Vision hints (`en,es`).
- `--unknown-label` – Prefix when no name is detected (default `Unknown Student`).

### Alternate Stage 1 – Qwen OCR (`openocr/ocr_tests.py`)
This CLI mirrors the Google-based batchocr workflow but swaps the OCR backend for Qwen (via OpenRouter). It reads `QWEN_API_KEY` and `QWEN_API_MODEL` from `.env` or the environment before sending each rasterized page for transcription.

- `--input/-i PATH` – PDF file, directory, or `-` for stdin (paths or raw PDF bytes).
- `--output/-o PATH` – JSONL output destination (default stdout).
- `--dpi INT` – Rasterization DPI passed to pdf2image (default 220).
- `--jpeg-quality INT` – JPEG quality for intermediate JPEGs (default 70).
- `--unknown-label TEXT` – Prefix for unidentified tests (default `Unknown Student`).
- `--prompt TEXT` – Instruction prepended to each image request (defaults to a generic transcription prompt; customize for handwriting emphasis).
- `--max-tokens INT` – Token cap for Qwen responses (default 2048).
- `--temperature FLOAT` – Qwen temperature (default 0.1 for deterministic OCR).
- `--api-url URL` – Override the OpenRouter endpoint (default `https://openrouter.ai/api/v1/chat/completions`).

Usage mirrors batchocr; for example:

```bash
cat scanned.pdf | python openocr/ocr_tests.py --prompt "Transcribe all handwriting verbatim"
```

Ensure the repository `.env` contains valid `QWEN_API_KEY` and `QWEN_API_MODEL` entries before running.

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

### Evaluation Rubric
The evaluator scores each essay on two criteria plus a total:
- **Criterion 1 – Question Response Quality (0–5 points)**: How well the student addresses the prompt, stays on topic, and responds directly.
- **Criterion 2 – Use of Evidence (0–5 points)**: Quality and relevance of supporting details or references to the reading material.
- **Total Score (0–10 points)**: Sum of the two criteria. Downstream tools can map this to a letter grade (default scale in `report_results.py` is 10 points = A).

`--material` / `--material-file` should supply the full reading passage or a detailed summary of the text the students were assessed on. `--question` / `--question-file` provides the actual test question (the prompt the essays respond to). Pairing both gives the evaluator enough context to judge whether the response matches the prompt.

### Alternate Stage 3 – Full Text Attachments (`evaluate/evaluate_fulltext.py`)
When the reading passage is too long to inline into the prompt, use `evaluate_fulltext.py`. It mirrors `evaluate_tests.py` but uploads the full PDF/TXT source files to the xAI Files API and references them in every batch.

- `--materials-path PATH` – **Required.** File or directory containing the full reading material to upload; directories are uploaded recursively and deleted after the run.
- `--question/--question-file` and `--context/--context-file` – Same as `evaluate_tests.py`, but the prompt reminds the model that evidence lives in the attachments.
- `--no-encrypted-content` – Optional switch to fall back to standard chats if your SDK/server does not support encrypted content.
- All other batching, model, timeout, env, and usage flags are identical to `evaluate_tests.py`.

Example: keep PDFs on disk while evaluating cleaned essays streaming from stdin:

```bash
python evaluate/evaluate_fulltext.py \
  --materials-path curriculum/Unit4/ReadingPack/ \
  --question-file prompts/unit4_question.txt \
  --context "Honors section rubric emphasis on textual citations." \
  --max-batch-tokens 10000 \
  --usage-metadata
```

## Rubrics for `evaluate_essay.py`

`evaluate_essay.py` consumes a rubric JSON that tells the AI how to grade each essay. The file must include a top-level `rubric` object with the following structure:

```json
{
  "rubric": {
    "course": "WR 121 - College Composition",
    "description": "Short description of what this rubric is for.",
    "scale": {
      "4": "Excellent",
      "3": "Good",
      "2": "Fair",
      "1": "Poor"
    },
    "criteria": [
      {
        "name": "Thesis and Argument",
        "description": "Evaluates clarity and strength of the central claim.",
        "levels": [
          { "score": 4, "label": "Excellent", "description": "..." },
          { "score": 3, "label": "Good", "description": "..." },
          { "score": 2, "label": "Fair", "description": "..." },
          { "score": 1, "label": "Poor", "description": "..." }
        ]
      }
      // 4–6 criteria total
    ],
    "total_points": 20
  }
}
```

Key requirements:

- **`rubric`**: required top-level key.
- **`rubric.course`**: non-empty string naming the course or assignment.
- **`rubric.description`**: non-empty string describing what the rubric measures.
- **`rubric.scale`**:
  - Maps stringified scores `"1"`–`"4"` to labels (e.g., `"Poor"`, `"Fair"`, `"Good"`, `"Excellent"`).
  - Scores must remain within the 1–4 range.
- **`rubric.criteria`**:
  - Non-empty list, typically 4–6 criteria.
  - Each criterion needs a `name`, `description`, and `levels`.
- **`levels`** (per criterion):
  - `score`: integer 1–4.
  - `label`: label for that score (falls back to `scale` if blank).
  - `description`: clear explanation of the performance at that score.
- **`rubric.total_points`**:
  - Optional integer max score. If omitted, the tool defaults to `len(criteria) * 4`.

Extra fields are allowed as long as the required structure is preserved. For example, adding a new criterion such as Refutation works when it follows the same pattern:

```json
{
  "name": "Refutation and Counterargument",
  "description": "Evaluates how well the writer anticipates and responds to opposing views.",
  "levels": [
    { "score": 4, "label": "Excellent", "description": "..." },
    { "score": 3, "label": "Good", "description": "..." },
    { "score": 2, "label": "Fair", "description": "..." },
    { "score": 1, "label": "Poor", "description": "..." }
  ]
}
```

Teachers are free to add, rename, or revise criteria as needed—just keep the number of criteria in the expected range (unless the CLI is updated) and ensure each `levels` block still covers the 1–4 scale. `evaluate_essay.py` uses these criterion names to align scores in its JSONL output.

## Running `evaluate_essay.py`

`evaluate_essay.py` ingests PDF essays (from a folder or ZIP), applies the rubric above, and emits JSONL plus an optional PDF summary. Basic usage:

```bash
python evaluate/evaluate_essay.py \
  --input-path path/to/essays/ \
  --rubric-file path/to/WR121_Rubric.json \
  --output evaluations.jsonl
```

Main flags:

- `--input-path/-i PATH`
  - Directory with `.pdf` files or a `.zip` containing PDFs. Each PDF becomes one essay.
- `--rubric-file PATH`
  - Path to the rubric JSON file described earlier.
- `--output/-o PATH`
  - Optional JSONL destination. Omit to stream to stdout.
- `--pdf-report PATH`
  - Optional path for a single instructor-facing PDF summarizing every evaluation.
- `--model NAME`
  - Optional Grok model override (defaults to `XAI_EVALUATE_MODEL` env var or the script constant, e.g., `grok-4-fast-reasoning`).
- `--max-batch-tokens INT`
  - Approximate token budget for each batch of essays.
- `--max-batch-size INT`
  - Maximum essays per batch sent to the model.
- `--timeout FLOAT`
  - HTTP timeout in seconds.
- `--env-file PATH`
  - Points to a `.env` with `XAI_API_KEY` and related settings (defaults to repo root `.env`).
- `--usage-metadata`
  - When set, attaches token usage under `metadata.evaluation_usage` for each JSONL record.

Example commands:

1. **Directory of PDFs, rubric, JSONL to stdout**

   ```bash
   python evaluate/evaluate_essay.py \
     --input-path essays/ \
     --rubric-file rubrics/WR121_Rubric.json
   ```

2. **ZIP of essays, JSONL + PDF report on disk**

   ```bash
   python evaluate/evaluate_essay.py \
     --input-path uploads/essays_batch1.zip \
     --rubric-file rubrics/WR121_Rubric.json \
     --output out/evaluations_WR121_batch1.jsonl \
     --pdf-report out/WR121_batch1_report.pdf
   ```

### Ensuring `student_name` detection

`evaluate_essay.py` cannot ask upstream stages for names, so it infers each `student_name` using three simple heuristics. To make sure the right name is captured, do **at least one** of the following when preparing each PDF:

1. Put the student's name on the first page with a clear `Name: Jane Doe` label (top margin is ideal). The script scans the first few non-empty lines and immediately uses anything after `Name:`.
2. If no label is available, ensure one of the first short lines contains a capitalized first-and-last name (e.g., `Jane Doe`). The CLI searches those early lines for a `First Last` pattern and adopts the first match.
3. As a fallback, the filename (minus extension) becomes the name after replacing `_`/`-` with spaces. Save files as `Jane_Doe.pdf` or `Jane-Doe.pdf` so this guess stays accurate if the PDF text is missing or messy.

Following one of the above keeps downstream reports, mail-merges, and roster matching aligned with the actual student.

The JSONL output includes:

- `student_name`
- `file_name`
- `summary`
- `rubric_scores` (one entry per rubric criterion with scores, labels, explanations, evidence)
- `total_score` and `max_score`
- `overall_comment`
- `improvement_tutorial` (advice plus original/improved excerpts)
- `metadata` (rubric info, optional usage data, etc.)

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

### Mailing List Format
Provide a UTF-8 CSV with headers `student-name,email-address`. The `student-name` must match the names carried through the pipeline (case-insensitive, spacing preserved). Example:

```
student-name,email-address
john doe,john.doe@example.com
jane doe,jane.doe@example.com
```

Hyphens, commas, and spacing are respected as written; ensure they line up with `student_name` values emitted by the evaluation/report stages.

## Stage 6 – Name Review (`namecorrector/name_corrector.py`)
- `--input/-i PATH` – JSONL input (default stdin).
- `--output/-o PATH` – JSONL output (default stdout).
- `--roster-file PATH` – Optional CSV/JSON with roster names/emails for suggested matches and automatic fills.
- `--env-file PATH` – Path to the shared `.env` (default repo root).

### Name Review Workflow
The name corrector inspects each record for delivery issues (missing or invalid email, failed match). Only those records pause for review. For each flagged student it:

- Displays the original student name, a short context snippet, and the upstream mail status.
- Suggests up to three likely roster name matches (when a roster is supplied).
- Lets the teacher enter a corrected name and, if necessary, supply an email manually.
- Updates `student_name`, `email`, `mail.status`, and `metadata.name_corrector` to mark the correction source for downstream tools.

When no records need attention the tool passes the stream through untouched.

## Stage 7 – SMTP Send (`mailroom/send_mail.py`)
- `--input/-i PATH` – JSONL file, directory of JSONL files, or `-` for stdin.
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
  | ./namecorrector/name_corrector.py --roster-file ~/Downloads/mail.csv \
  | ./mailroom/send_mail.py --attach-report --report out/mailroom/send_report.txt
```

All scripts respect the shared `.env` in the repository root for credentials and defaults. Each stage prints concise start/finish messages to stderr so you can monitor progress while the JSON flows through stdout for further processing.
