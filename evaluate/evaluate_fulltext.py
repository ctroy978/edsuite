#!/usr/bin/env python
"""
CLI variant of evaluate_tests that keeps the full source material external to the
prompt by uploading it via the xAI Files API and referencing it in chat.

The workflow mirrors evaluate_tests.py, but --materials-path points to either a
single file or a directory tree of PDF/TXT resources that should be attached to
each evaluation batch. The files are deleted when processing finishes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from xai_sdk import Client
from xai_sdk.chat import file as file_attachment
from xai_sdk.chat import user

from evaluate_tests import (
    DEFAULT_MAX_BATCH_SIZE,
    DEFAULT_MAX_BATCH_TOKENS,
    Submission,
    align_batch,
    assemble_prompt,
    build_batches,
    build_prompt_header,
    format_essay_block,
    iter_input_records,
    parse_evaluations,
    prepare_submissions,
    resolve_resource,
    load_env_file,
)


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT_DIR / ".env"


@dataclass
class UploadedMaterial:
    path: Path
    file_id: str


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate cleaned essays via xAI Grok with attached source files and emit JSONL results.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", "-i", default="-", help="JSONL input file or '-' for stdin")
    parser.add_argument("--output", "-o", default=None, help="Write JSONL results to this file")
    parser.add_argument(
        "--materials-path",
        required=True,
        help="Path to a file or directory containing the full reading material to upload",
    )
    parser.add_argument("--question", default=None, help="Essay question as a raw string")
    parser.add_argument("--question-file", dest="question_file", default=None, help="Path to essay question text/PDF")
    parser.add_argument("--context", default="", help="Additional context string for the evaluator")
    parser.add_argument("--context-file", dest="context_file", default=None, help="Path to context text/PDF")
    parser.add_argument("--model", default=None, help="xAI Grok model name")
    parser.add_argument(
        "--max-batch-tokens",
        type=int,
        default=DEFAULT_MAX_BATCH_TOKENS,
        help="Approximate token budget per batch (still accounts for essay text)",
    )
    parser.add_argument("--max-batch-size", type=int, default=DEFAULT_MAX_BATCH_SIZE, help="Maximum number of essays per batch")
    parser.add_argument("--timeout", type=float, default=180.0, help="HTTP timeout (seconds)")
    parser.add_argument("--env-file", default=str(ENV_FILE), help="Path to .env file with API credentials")
    parser.add_argument(
        "--usage-metadata",
        action="store_true",
        help="Attach token usage per batch to output metadata when provided by the API",
    )
    parser.add_argument(
        "--no-encrypted-content",
        dest="use_encrypted_content",
        action="store_false",
        help="Disable encrypted content optimization when creating chats",
    )
    parser.set_defaults(use_encrypted_content=True)
    return parser.parse_args(argv)


def discover_material_files(path_str: str) -> List[Path]:
    target = Path(path_str).expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(f"Materials path not found: {target}")
    if target.is_file():
        return [target]
    files = sorted(p for p in target.rglob("*") if p.is_file())
    if not files:
        raise ValueError(f"No files discovered under {target}")
    return files


def upload_material_files(client: Client, files: Sequence[Path]) -> List[UploadedMaterial]:
    uploaded: List[UploadedMaterial] = []
    try:
        for file_path in files:
            print(f"[evaluate_fulltext] Uploading material: {file_path}", file=sys.stderr)
            resource = client.files.upload(str(file_path))
            file_id = getattr(resource, "id", None)
            if not file_id:
                raise RuntimeError(f"No file ID returned for upload: {file_path}")
            uploaded.append(UploadedMaterial(path=file_path, file_id=file_id))
    except Exception:
        if uploaded:
            delete_uploaded_files(client, uploaded)
        raise
    return uploaded


def delete_uploaded_files(client: Client, uploaded: Sequence[UploadedMaterial]) -> None:
    for item in uploaded:
        try:
            client.files.delete(item.file_id)
            print(f"[evaluate_fulltext] Deleted uploaded material: {item.path}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - best effort cleanup
            print(f"[evaluate_fulltext] Failed to delete {item.file_id}: {exc}", file=sys.stderr)


def normalize_usage(usage_obj: Any) -> Dict[str, float]:
    if not usage_obj:
        return {}
    if isinstance(usage_obj, dict):
        return {k: float(v) for k, v in usage_obj.items() if isinstance(v, (int, float))}
    model_dump = getattr(usage_obj, "model_dump", None)
    if callable(model_dump):
        return {k: float(v) for k, v in model_dump().items() if isinstance(v, (int, float))}
    if isinstance(usage_obj, str):
        return {}
    if isinstance(usage_obj, Iterable):
        result: Dict[str, float] = {}
        for item in usage_obj:  # type: ignore[assignment]
            if isinstance(item, tuple) and len(item) == 2:
                key, value = item
                if isinstance(key, str) and isinstance(value, (int, float)):
                    result[key] = float(value)
        return result
    return {}


def evaluate_batch(
    client: Client,
    model_name: str,
    prompt: str,
    attachments: Sequence[Any],
    *,
    use_encrypted_content: bool,
) -> Tuple[str, Dict[str, float]]:
    chat_kwargs: Dict[str, Any] = {"model": model_name}
    if use_encrypted_content:
        chat_kwargs["use_encrypted_content"] = True
    chat = client.chat.create(**chat_kwargs)
    chat.append(user(prompt, *attachments))
    response = chat.sample()

    content = getattr(response, "content", None)
    if not content:
        raise RuntimeError("No content returned from evaluation response")
    usage = normalize_usage(getattr(response, "usage", None))
    return str(content), usage


def build_material_placeholder(file_count: int) -> str:
    plural = "s" if file_count != 1 else ""
    return f"[Reading material provided via {file_count} attached file{plural}. Consult the attachments for the full text.]"


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    print("[evaluate_fulltext] Starting evaluation...", file=sys.stderr)
    load_env_file(Path(args.env_file))

    try:
        question = resolve_resource(args.question, args.question_file)
        context = resolve_resource(args.context, args.context_file, allow_empty=True)
    except Exception as error:  # noqa: BLE001
        print(f"Failed to load resources: {error}", file=sys.stderr)
        return 1

    try:
        material_files = discover_material_files(args.materials_path)
    except Exception as error:  # noqa: BLE001
        print(f"Failed to discover material files: {error}", file=sys.stderr)
        return 1

    submissions = prepare_submissions(iter_input_records(args.input))
    if not submissions:
        print("No valid submissions to evaluate", file=sys.stderr)
        return 1

    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        print("Missing XAI_API_KEY in environment or .env", file=sys.stderr)
        return 1

    client_kwargs: Dict[str, Any] = {"api_key": api_key}
    optional_kwargs: List[Tuple[str, Any]] = []
    base_url = os.environ.get("XAI_BASE_URL")
    if base_url:
        optional_kwargs.append(("base_url", base_url))
    if args.timeout:
        optional_kwargs.append(("timeout", args.timeout))
    for key, value in optional_kwargs:
        client_kwargs[key] = value

    while True:
        try:
            client = Client(**client_kwargs)
            break
        except TypeError as error:
            if not optional_kwargs:
                raise error
            key, value = optional_kwargs.pop()
            if key in client_kwargs:
                client_kwargs.pop(key, None)
                print(f"[evaluate_fulltext] {key} not supported by SDK, retrying without ({value})", file=sys.stderr)
                continue
            raise

    uploaded: List[UploadedMaterial] = []
    output_stream = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    managed_output = output_stream is not sys.stdout
    aggregated_usage: Dict[str, float] = {}
    model_name = args.model or os.environ.get("XAI_EVALUATE_MODEL") or "grok-4-fast"
    exit_code = 0

    try:
        uploaded = upload_material_files(client, material_files)
        attachments = [file_attachment(item.file_id) for item in uploaded]
        material_placeholder = build_material_placeholder(len(uploaded))
        prompt_header = build_prompt_header(material_placeholder, question, context)
        prompt_header += (
            "\nThe complete reading material is included as attached files in this message. "
            "Use them when citing evidence or details.\n"
        )
        batches = build_batches(submissions, prompt_header, args.max_batch_tokens, args.max_batch_size)

        for batch_index, batch in enumerate(batches, start=1):
            prompt = assemble_prompt(prompt_header, batch)
            try:
                raw_content, usage = evaluate_batch(
                    client,
                    model_name=model_name,
                    prompt=prompt,
                    attachments=attachments,
                    use_encrypted_content=args.use_encrypted_content,
                )
            except Exception as error:  # noqa: BLE001
                exit_code = 1
                print(f"Batch {batch_index} evaluation failed: {error}", file=sys.stderr)
                continue

            try:
                evaluations = parse_evaluations(raw_content)
            except Exception as error:  # noqa: BLE001
                exit_code = 1
                print(f"Batch {batch_index} JSON parse failed: {error}", file=sys.stderr)
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
                aggregated_usage[key] = aggregated_usage.get(key, 0.0) + float(value)
    finally:
        if uploaded:
            delete_uploaded_files(client, uploaded)
        if managed_output:
            output_stream.close()

    print(f"[evaluate_fulltext] Completed evaluation (exit={exit_code}).", file=sys.stderr)
    if aggregated_usage:
        def format_usage_value(value: float) -> str:
            return str(int(value)) if float(value).is_integer() else f"{value:.2f}"

        usage_summary = ", ".join(f"{key}={format_usage_value(value)}" for key, value in aggregated_usage.items())
        print(f"Aggregated token usage: {usage_summary}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
