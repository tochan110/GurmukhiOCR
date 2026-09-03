#!/usr/bin/env python3
"""Proof-of-concept Google Cloud Vision async PDF OCR test.

Usage:
    python vision_async_test.py /path/to/file.pdf

The script reuses the app's local credential convention:
GURMUKHI_OCR_KEY_PATH first, then GOOGLE_APPLICATION_CREDENTIALS.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - convenience for ad-hoc environments.
    load_dotenv = None


DEFAULT_BUCKET = "gocr-processing-1"
DEFAULT_PREFIX = "vision_async_test"
SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
GCS_BASE_URL = "https://storage.googleapis.com/storage/v1"
GCS_UPLOAD_URL = "https://storage.googleapis.com/upload/storage/v1"
EXPECTED_UPLOAD = "5-15 s"
EXPECTED_OCR = "3-10 minutes"
EXPECTED_DOWNLOAD = "<30 s"


def load_google_dependencies() -> tuple[Any, Any, Any]:
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.cloud import vision
        from google.oauth2 import service_account
    except ImportError as exc:
        raise RuntimeError(
            "Missing Google Cloud dependencies. Activate the project virtualenv and run "
            "`pip install -r requirements.txt` before executing this proof-of-concept script."
        ) from exc
    return AuthorizedSession, vision, service_account


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload a PDF, run Vision async DOCUMENT_TEXT_DETECTION, print OCR JSON structure, then clean up GCS files."
    )
    parser.add_argument("pdf_path", help="Local PDF file to OCR.")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET, help=f"GCS bucket to use. Default: {DEFAULT_BUCKET}")
    parser.add_argument(
        "--credentials",
        help="Path to a Google service account JSON file. Defaults to GURMUKHI_OCR_KEY_PATH or GOOGLE_APPLICATION_CREDENTIALS.",
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help=f"Base GCS prefix for this proof-of-concept run. Default: {DEFAULT_PREFIX}",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Vision output pages per JSON file. Default: 20",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Seconds to wait for the Vision operation. Default: 1800",
    )
    parser.add_argument(
        "--keep-gcs-files",
        action="store_true",
        help="Skip cleanup so uploaded/input and OCR output objects remain in the bucket for debugging.",
    )
    parser.add_argument(
        "--sample-pages",
        type=int,
        default=3,
        help="Number of OCR page responses to keep in a local sample JSON file. Use 0 to disable. Default: 3",
    )
    parser.add_argument(
        "--sample-output",
        help="Local path for the sample JSON file. Default: vision_async_sample_pages_<run_id>.json",
    )
    return parser.parse_args()


def resolve_credentials_path(explicit_path: str | None) -> Path:
    candidates: list[tuple[str, str]] = []
    if explicit_path:
        candidates.append(("--credentials", explicit_path))
    else:
        for env_name in ("GURMUKHI_OCR_KEY_PATH", "GOOGLE_APPLICATION_CREDENTIALS"):
            value = (os.environ.get(env_name) or "").strip()
            if value:
                candidates.append((env_name, value))

    for source, value in candidates:
        path = Path(value).expanduser()
        if path.is_file():
            return path
        print(f"Credential path from {source} does not exist or is not a file: {path}", file=sys.stderr)

    raise RuntimeError(
        "No Google service account JSON found. Set GURMUKHI_OCR_KEY_PATH, "
        "GOOGLE_APPLICATION_CREDENTIALS, or pass --credentials."
    )


def make_run_paths(base_prefix: str, pdf_path: Path) -> tuple[str, str, str, str]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = uuid.uuid4().hex[:12]
    prefix = base_prefix.strip("/")
    run_prefix = f"{prefix}/{timestamp}-{run_id}/" if prefix else f"{timestamp}-{run_id}/"
    input_object = f"{run_prefix}input/{pdf_path.name}"
    output_prefix = f"{run_prefix}output/"
    return run_id, run_prefix, input_object, output_prefix


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes = int(seconds // 60)
    remaining_seconds = seconds % 60
    return f"{minutes} min {remaining_seconds:.1f} s"


def print_timing_summary(timings: dict[str, tuple[float, str]]) -> None:
    if not timings:
        return

    print("\nTiming summary")
    for label in ("Upload", "OCR", "Download JSON"):
        if label in timings:
            elapsed_seconds, expected = timings[label]
            print(f"{label}: {format_duration(elapsed_seconds)} (expected {expected})")


def gcs_object_url(bucket: str, object_name: str) -> str:
    return f"{GCS_BASE_URL}/b/{quote(bucket, safe='')}/o/{quote(object_name, safe='')}"


def upload_pdf(session: Any, bucket: str, object_name: str, pdf_path: Path) -> None:
    url = f"{GCS_UPLOAD_URL}/b/{quote(bucket, safe='')}/o"
    with pdf_path.open("rb") as pdf_file:
        response = session.post(
            url,
            params={"uploadType": "media", "name": object_name},
            headers={"Content-Type": "application/pdf"},
            data=pdf_file,
        )

    if not response.ok:
        print("Upload failed")
        print(response.status_code)
        print(response.text)
        response.raise_for_status()


def list_objects(session: Any, bucket: str, prefix: str) -> list[str]:
    url = f"{GCS_BASE_URL}/b/{quote(bucket, safe='')}/o"
    names: list[str] = []
    page_token: str | None = None
    while True:
        params = {"prefix": prefix}
        if page_token:
            params["pageToken"] = page_token
        response = session.get(url, params=params)
        if not response.ok:
            print("Upload failed")
            print(response.status_code)
            print(response.text)
            response.raise_for_status()
        payload = response.json()
        names.extend(item["name"] for item in payload.get("items", []))
        page_token = payload.get("nextPageToken")
        if not page_token:
            return names


def download_object(session: Any, bucket: str, object_name: str, destination: Path) -> None:
    response = session.get(gcs_object_url(bucket, object_name), params={"alt": "media"})
    response.raise_for_status()
    destination.write_bytes(response.content)


def delete_object(session: Any, bucket: str, object_name: str) -> None:
    response = session.delete(gcs_object_url(bucket, object_name))
    if response.status_code == 404:
        return
    response.raise_for_status()


def submit_vision_operation(
    client: Any,
    vision_module: Any,
    source_uri: str,
    output_uri: str,
    batch_size: int,
):
    feature = vision_module.Feature(type_=vision_module.Feature.Type.DOCUMENT_TEXT_DETECTION)
    gcs_source = vision_module.GcsSource(uri=source_uri)
    input_config = vision_module.InputConfig(gcs_source=gcs_source, mime_type="application/pdf")
    gcs_destination = vision_module.GcsDestination(uri=output_uri)
    output_config = vision_module.OutputConfig(gcs_destination=gcs_destination, batch_size=batch_size)
    request = vision_module.AsyncAnnotateFileRequest(
        features=[feature],
        input_config=input_config,
        output_config=output_config,
    )
    return client.async_batch_annotate_files(requests=[request])


def operation_name(operation: Any) -> str:
    wrapped_operation = getattr(operation, "operation", None)
    name = getattr(wrapped_operation, "name", None) or getattr(operation, "name", None)
    return str(name) if name else "(operation name unavailable)"


def local_name_for_object(object_name: str) -> str:
    return object_name.strip("/").replace("/", "__") or "ocr-output.json"


def response_pages_from_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    for document in documents:
        document_responses = document.get("responses", [])
        if isinstance(document_responses, list):
            responses.extend(item for item in document_responses if isinstance(item, dict))
    return responses


def load_downloaded_json_outputs(
    session: Any,
    bucket: str,
    object_names: list[str],
    download_dir: Path,
) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for object_name in object_names:
        local_path = download_dir / local_name_for_object(object_name)
        download_object(session, bucket, object_name, local_path)
        with local_path.open("r", encoding="utf-8") as f:
            documents.append(json.load(f))
        print(f"Downloaded OCR JSON: gs://{bucket}/{object_name}")
    return documents


def response_text(response: dict[str, Any]) -> str:
    full_text = response.get("fullTextAnnotation", {}).get("text")
    if full_text:
        return str(full_text)

    annotations = response.get("textAnnotations") or []
    if annotations and isinstance(annotations[0], dict):
        return str(annotations[0].get("description") or "")
    return ""


def print_json_structure(documents: list[dict[str, Any]]) -> None:
    if not documents:
        print("No OCR JSON documents were downloaded.")
        return

    responses = response_pages_from_documents(documents)

    sample_text = ""
    sample_index = None
    for index, response in enumerate(responses, start=1):
        text = response_text(response).strip()
        if text:
            sample_text = " ".join(text.split())
            sample_index = index
            break

    top_level_keys = sorted(documents[0].keys())
    response_errors = sum(1 for response in responses if response.get("error"))

    print("\nOCR JSON structure")
    print(f"Top-level keys in first JSON file: {top_level_keys}")
    print(f"OCR JSON files downloaded: {len(documents)}")
    print(f"Total pages/responses: {len(responses)}")
    if response_errors:
        print(f"Responses with errors: {response_errors}")

    if sample_text:
        print(f"\nSample text from response/page {sample_index}:")
        print(sample_text[:1000])
    else:
        print("\nNo text found in the downloaded OCR responses.")


def resolve_sample_output_path(explicit_path: str | None, run_id: str) -> Path:
    if explicit_path:
        return Path(explicit_path).expanduser()
    return Path.cwd() / f"vision_async_sample_pages_{run_id}.json"


def save_sample_pages(
    documents: list[dict[str, Any]],
    sample_output_path: Path,
    sample_pages: int,
    run_id: str,
    source_uri: str,
    output_uri: str,
) -> None:
    responses = response_pages_from_documents(documents)
    sample_payload = {
        "run_id": run_id,
        "source_uri": source_uri,
        "vision_output_uri": output_uri,
        "sample_pages_requested": sample_pages,
        "sample_pages_saved": min(sample_pages, len(responses)),
        "total_pages_responses": len(responses),
        "responses": responses[:sample_pages],
    }

    sample_output_path.parent.mkdir(parents=True, exist_ok=True)
    with sample_output_path.open("w", encoding="utf-8") as f:
        json.dump(sample_payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"\nSaved sample JSON ({sample_payload['sample_pages_saved']} pages): {sample_output_path}")


def cleanup_gcs_files(session: Any, bucket: str, object_names: set[str]) -> None:
    for object_name in sorted(object_names):
        try:
            delete_object(session, bucket, object_name)
            print(f"Deleted: gs://{bucket}/{object_name}")
        except Exception as exc:
            print(f"Warning: failed to delete gs://{bucket}/{object_name}: {exc}", file=sys.stderr)


def main() -> int:
    args = parse_args()

    if load_dotenv:
        load_dotenv()

    pdf_path = Path(args.pdf_path).expanduser()
    if not pdf_path.is_file():
        raise RuntimeError(f"PDF path does not exist or is not a file: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise RuntimeError(f"Expected a .pdf file, got: {pdf_path}")
    if args.batch_size < 1:
        raise RuntimeError("--batch-size must be at least 1.")
    if args.sample_pages < 0:
        raise RuntimeError("--sample-pages must be 0 or greater.")

    authorized_session_cls, vision_module, service_account_module = load_google_dependencies()
    credentials_path = resolve_credentials_path(args.credentials)
    credentials = service_account_module.Credentials.from_service_account_file(
        str(credentials_path),
        scopes=SCOPES,
    )

    print(f"Project: {credentials.project_id}")
    print(f"Service account: {credentials.service_account_email}")

    storage_session = authorized_session_cls(credentials)
    vision_client = vision_module.ImageAnnotatorClient(credentials=credentials)

    run_id, run_prefix, input_object, output_prefix = make_run_paths(args.prefix, pdf_path)
    source_uri = f"gs://{args.bucket}/{input_object}"
    output_uri = f"gs://{args.bucket}/{output_prefix}"
    cleanup_objects = {input_object}
    timings: dict[str, tuple[float, str]] = {}

    print(f"Run id: {run_id}")
    print(f"Bucket: {args.bucket}")
    print(f"Run prefix: {run_prefix}")
    print(f"Credentials: {credentials_path}")
    print("\nTiming expectations")
    print(f"Upload: {EXPECTED_UPLOAD}")
    print(f"OCR: {EXPECTED_OCR}")
    print(f"Download JSON: {EXPECTED_DOWNLOAD}")

    try:
        print(f"\nUploading PDF to {source_uri}")
        upload_started = time.perf_counter()
        upload_pdf(storage_session, args.bucket, input_object, pdf_path)
        timings["Upload"] = (time.perf_counter() - upload_started, EXPECTED_UPLOAD)
        print("Upload completed")

        print("\nSubmitting Vision asyncBatchAnnotateFiles request")
        ocr_started = time.perf_counter()
        operation = submit_vision_operation(
            vision_client,
            vision_module,
            source_uri=source_uri,
            output_uri=output_uri,
            batch_size=args.batch_size,
        )
        print(f"Operation: {operation_name(operation)}")
        print(f"Waiting for completion, timeout={args.timeout}s")
        operation.result(timeout=args.timeout)
        print("Vision operation completed")
        timings["OCR"] = (time.perf_counter() - ocr_started, EXPECTED_OCR)

        print("\nDownloading OCR JSON output")
        download_started = time.perf_counter()
        output_objects = sorted(list_objects(storage_session, args.bucket, output_prefix))
        cleanup_objects.update(output_objects)
        json_objects = [name for name in output_objects if name.lower().endswith(".json")]
        if not json_objects:
            raise RuntimeError(f"No JSON output found under {output_uri}")

        with tempfile.TemporaryDirectory(prefix="vision_async_test_") as temp_dir:
            documents = load_downloaded_json_outputs(
                storage_session,
                args.bucket,
                json_objects,
                Path(temp_dir),
            )
            timings["Download JSON"] = (time.perf_counter() - download_started, EXPECTED_DOWNLOAD)
            print_json_structure(documents)
            if args.sample_pages:
                save_sample_pages(
                    documents,
                    resolve_sample_output_path(args.sample_output, run_id),
                    args.sample_pages,
                    run_id,
                    source_uri,
                    output_uri,
                )
    finally:
        if args.keep_gcs_files:
            print("\nSkipping GCS cleanup because --keep-gcs-files was provided.")
            print(f"Temporary files remain under: gs://{args.bucket}/{run_prefix}")
        else:
            try:
                cleanup_objects.update(list_objects(storage_session, args.bucket, output_prefix))
            except Exception as exc:
                print(f"Warning: failed to list output files for cleanup: {exc}", file=sys.stderr)
            print("\nCleaning up temporary GCS files")
            cleanup_gcs_files(storage_session, args.bucket, cleanup_objects)
        print_timing_summary(timings)

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
