#!/usr/bin/env python3
"""Convert Google Vision async OCR JSON into page-separated plain text.

Usage:
    python3 vision_json_to_text.py vision_async_sample_pages_abc123.json
    python3 vision_json_to_text.py output-*.json --output ocr_pages.txt
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract page text from Google Vision OCR JSON and write page-separated plain text."
    )
    parser.add_argument("json_paths", nargs="+", help="Vision OCR JSON file(s) to convert.")
    parser.add_argument(
        "-o",
        "--output",
        help="Text output path. Defaults to <first-json-stem>.txt, or vision_ocr_pages.txt for multiple inputs.",
    )
    parser.add_argument("--stdout", action="store_true", help="Print text to stdout instead of writing a file.")
    return parser.parse_args()


def default_output_path(json_paths: list[Path]) -> Path:
    if len(json_paths) == 1:
        return json_paths[0].with_suffix(".txt")
    return Path.cwd() / "vision_ocr_pages.txt"


def load_responses(json_path: Path) -> list[dict[str, Any]]:
    with json_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    responses = payload.get("responses") if isinstance(payload, dict) else None
    if not isinstance(responses, list):
        raise RuntimeError(f"{json_path} does not contain a top-level 'responses' list.")
    return [response for response in responses if isinstance(response, dict)]


def break_text(break_type: str | None) -> str:
    if break_type in {"SPACE", "SURE_SPACE"}:
        return " "
    if break_type in {"EOL_SURE_SPACE", "LINE_BREAK"}:
        return "\n"
    return ""


def reconstruct_text_from_symbols(response: dict[str, Any]) -> str:
    pages = response.get("fullTextAnnotation", {}).get("pages", [])
    text_parts: list[str] = []

    for page in pages if isinstance(pages, list) else []:
        for block in page.get("blocks", []):
            for paragraph in block.get("paragraphs", []):
                for word in paragraph.get("words", []):
                    for symbol in word.get("symbols", []):
                        text_parts.append(str(symbol.get("text") or ""))
                        detected_break = symbol.get("property", {}).get("detectedBreak", {})
                        text_parts.append(break_text(detected_break.get("type")))
                if text_parts and not text_parts[-1].endswith("\n"):
                    text_parts.append("\n")
            if text_parts and not text_parts[-1].endswith("\n\n"):
                text_parts.append("\n")

    return "".join(text_parts).strip()


def response_text(response: dict[str, Any]) -> str:
    error = response.get("error")
    if error:
        return f"[Vision error: {error}]"

    full_text = response.get("fullTextAnnotation", {}).get("text")
    if full_text:
        return str(full_text).strip()

    annotations = response.get("textAnnotations") or []
    if annotations and isinstance(annotations[0], dict):
        description = annotations[0].get("description")
        if description:
            return str(description).strip()

    return reconstruct_text_from_symbols(response)


def convert_json_files(json_paths: list[Path]) -> tuple[str, int]:
    pages: list[str] = []
    page_number = 1

    for json_path in json_paths:
        for response in load_responses(json_path):
            text = response_text(response)
            pages.append(f"========== Page {page_number} ==========\n{text}\n")
            page_number += 1

    return "\n".join(pages), len(pages)


def main() -> int:
    args = parse_args()
    json_paths = [Path(path).expanduser() for path in args.json_paths]
    missing = [path for path in json_paths if not path.is_file()]
    if missing:
        raise RuntimeError(f"JSON file not found: {missing[0]}")

    output_text, page_count = convert_json_files(json_paths)
    if args.stdout:
        print(output_text)
        print(f"\nConverted {page_count} page(s).", file=sys.stderr)
        return 0

    output_path = Path(args.output).expanduser() if args.output else default_output_path(json_paths)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output_text, encoding="utf-8")
    print(f"Wrote {page_count} page(s) to: {output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
