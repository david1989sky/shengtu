#!/usr/bin/env python3
"""Generate or edit images through the Subarx image gateway API."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import stat
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


DEFAULT_BASE_URL = "https://st.subarx.com"
API_KEY_ENV_NAMES = ("SUBARX_IMAGE_API_KEY", "SUBARX_API_KEY", "AISTATION_API_KEY", "AIWANWU_API_KEY")
CONFIG_FILE_NAME = "config.json"
DEFAULT_OUTPUT_DIR_NAME = "生图"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 300
DEFAULT_IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 180
DEFAULT_MAX_RETRIES = 2
RETRYABLE_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def config_dir() -> Path:
    if os.name == "nt":
        root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(root) / "Codex" / "skills" / "shengtu-skill"
    root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root) / "codex" / "skills" / "shengtu-skill"


def config_path() -> Path:
    return config_dir() / CONFIG_FILE_NAME


def windows_user_env(name: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value) if value else None
    except OSError:
        return None


def read_config_api_key() -> str | None:
    path = config_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = str(data.get("api_key") or "").strip()
    return value or None


def write_config_api_key(api_key: str) -> Path:
    value = str(api_key or "").strip()
    if not value:
        raise ValueError("api_key is empty")
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"api_key": value}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def clear_config_api_key() -> bool:
    path = config_path()
    if not path.exists():
        return False
    path.unlink()
    return True


def env_api_key() -> str | None:
    for name in API_KEY_ENV_NAMES:
        value = os.environ.get(name) or windows_user_env(name)
        if value:
            return value
    return None


def resolved_api_key(cli_api_key: str | None = None) -> str | None:
    return str(cli_api_key or "").strip() or env_api_key() or read_config_api_key()


def resolved_base_url(cli_base_url: str | None = None) -> str:
    return str(cli_base_url or "").strip() or DEFAULT_BASE_URL


def default_output_dir() -> Path:
    return Path.home() / "Desktop" / DEFAULT_OUTPUT_DIR_NAME


def resolved_output_path(cli_out: str | None, output_format: str = "png") -> Path:
    value = str(cli_out or "").strip()
    if value:
        return Path(value)
    suffix = str(output_format or "png").strip().lower().lstrip(".") or "png"
    directory = default_output_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"shengtu-{datetime.now().strftime('%Y%m%d-%H%M%S')}.{suffix}"


def should_retry_http_error(exc: urllib.error.HTTPError) -> bool:
    return exc.code in RETRYABLE_HTTP_STATUS_CODES


def backoff_seconds(attempt: int) -> float:
    return float(attempt)


def now_ms() -> int:
    return int(time.time() * 1000)


def default_debug_log(message: str) -> None:
    print(message, file=sys.stderr)


def is_data_url(value: str) -> bool:
    return value.startswith("data:")


def decode_data_url(value: str) -> bytes:
    try:
        header, encoded = value.split(",", 1)
    except ValueError as exc:
        raise RuntimeError("Invalid data URL image payload") from exc
    if ";base64" not in header:
        raise RuntimeError("Only base64 data URL image payloads are supported")
    return base64.b64decode(encoded)


def non_empty_string(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def image_carrier_from_item(item: dict) -> tuple[str, str] | None:
    for key in ("b64_json", "base64", "image", "data"):
        value = non_empty_string(item.get(key))
        if value:
            return ("data_url" if is_data_url(value) else key, value)

    value = non_empty_string(item.get("url") or item.get("image_url"))
    if value:
        return ("data_url" if is_data_url(value) else "url", value)

    result = item.get("result")
    if isinstance(result, str) and result.strip():
        value = result.strip()
        return ("data_url" if is_data_url(value) else "result", value)
    if isinstance(result, dict):
        return image_carrier_from_item(result)

    return None


def iter_image_items(result: dict):
    data = result.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield "images", item

    output = result.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") and item.get("type") != "image_generation_call":
                continue
            yield "responses", item


def find_first_image_carrier(result: dict) -> tuple[str, str, str] | None:
    for shape, item in iter_image_items(result):
        carrier = image_carrier_from_item(item)
        if carrier:
            kind, value = carrier
            return shape, kind, value
    return None


def describe_result(result: dict) -> str:
    request_id = str(result.get("request_id") or result.get("id") or "").strip() or "-"
    shape = "unknown"
    items = 0
    first_item = "-"
    for item_shape, item in iter_image_items(result):
        if shape == "unknown":
            shape = item_shape
        items += 1
        if first_item == "-":
            carrier = image_carrier_from_item(item)
            first_item = carrier[0] if carrier else "unknown"
    return f"request_id={request_id} shape={shape} items={items} first_item={first_item}"


def perform_request(
    req: urllib.request.Request,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    debug_log=None,
) -> dict:
    last_error: RuntimeError | None = None
    debug = debug_log or (lambda _message: None)
    started_at = now_ms()
    for attempt in range(max_retries + 1):
        attempt_started_at = now_ms()
        debug(f"attempt {attempt + 1}/{max_retries + 1} method={req.method} url={req.full_url}")
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS) as resp:
                response_started_at = now_ms()
                raw = resp.read().decode("utf-8")
                result = json.loads(raw)
                headers = getattr(resp, "headers", {}) or {}
                x_request_id = "-"
                if hasattr(headers, "get"):
                    x_request_id = headers.get("x-request-id", "-")
                debug(
                    "response "
                    f"status={getattr(resp, 'status', '-')} "
                    f"attempt_elapsed_ms={response_started_at - attempt_started_at} "
                    f"total_elapsed_ms={response_started_at - started_at} "
                    f"x_request_id={x_request_id} "
                    f"{describe_result(result)}"
                )
                return result
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"HTTP {exc.code}: {detail}")
            debug(
                f"http_error status={exc.code} "
                f"attempt_elapsed_ms={now_ms() - attempt_started_at} "
                f"retryable={should_retry_http_error(exc)}"
            )
            if attempt >= max_retries or not should_retry_http_error(exc):
                raise last_error from exc
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f"Network error: {exc}")
            debug(f"network_error attempt_elapsed_ms={now_ms() - attempt_started_at} error={exc}")
            if attempt >= max_retries:
                raise last_error from exc

        time.sleep(backoff_seconds(attempt + 1))

    raise last_error or RuntimeError("request failed")


def request_json(url: str, api_key: str, payload: dict, *, debug_log=None) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    return perform_request(req, debug_log=debug_log)


def request_multipart(url: str, api_key: str, fields: dict[str, str], files: list[tuple[str, Path]], *, debug_log=None) -> dict:
    boundary = "----shengtu-skill-boundary"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")

    for field_name, path in files:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{path.name}"\r\n'.encode("utf-8")
        )
        chunks.append(f"Content-Type: {mime}\r\n\r\n".encode("utf-8"))
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(chunks)
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    return perform_request(req, debug_log=debug_log)


def download_image_url(image_url: str, *, debug_log=None) -> bytes:
    debug = debug_log or (lambda _message: None)
    last_error: RuntimeError | None = None
    for attempt in range(DEFAULT_MAX_RETRIES + 1):
        started_at = now_ms()
        debug(f"download attempt {attempt + 1}/{DEFAULT_MAX_RETRIES + 1} url={image_url}")
        try:
            with urllib.request.urlopen(image_url, timeout=DEFAULT_IMAGE_DOWNLOAD_TIMEOUT_SECONDS) as resp:
                raw = resp.read()
                debug(
                    "download_response "
                    f"status={getattr(resp, 'status', '-')} "
                    f"elapsed_ms={now_ms() - started_at} "
                    f"bytes={len(raw)}"
                )
                return raw
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"Image URL HTTP {exc.code}: {detail}")
            retryable = should_retry_http_error(exc)
            debug(f"download_http_error status={exc.code} elapsed_ms={now_ms() - started_at} retryable={retryable}")
            if attempt >= DEFAULT_MAX_RETRIES or not retryable:
                raise last_error from exc
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f"Image URL network error: {exc}")
            debug(f"download_error elapsed_ms={now_ms() - started_at} error={exc}")
            if attempt >= DEFAULT_MAX_RETRIES:
                raise last_error from exc
        time.sleep(backoff_seconds(attempt + 1))
    raise last_error or RuntimeError("image URL download failed")


def save_image(result: dict, out_path: Path, *, debug_log=None) -> None:
    carrier = find_first_image_carrier(result)
    if not carrier:
        raise RuntimeError(f"Response has no recognizable image payload: {json.dumps(result, ensure_ascii=False)[:1000]}")

    shape, kind, value = carrier
    debug = debug_log or (lambda _message: None)
    debug(f"image_source shape={shape} kind={kind}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if kind == "data_url":
        out_path.write_bytes(decode_data_url(value))
        return

    if kind == "url":
        out_path.write_bytes(download_image_url(value, debug_log=debug_log))
        return

    out_path.write_bytes(base64.b64decode(value))


def main() -> int:
    parser = argparse.ArgumentParser(description="Call the Subarx image gateway API and save the result.")
    parser.add_argument("--prompt")
    parser.add_argument("--out", help='Output image path, usually .png. Defaults to ~/Desktop/生图/shengtu-YYYYMMDD-HHMMSS.png')
    parser.add_argument("--size", default="1024x1024", help="Resolution string such as 1024x1024")
    parser.add_argument("--mode", choices=("generate", "edit"), default="generate")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--save-api-key", help="Save a dedicated Subarx image API key to this skill's private config")
    parser.add_argument("--clear-api-key", action="store_true", help="Remove this skill's saved API key")
    parser.add_argument("--show-config-path", action="store_true", help="Print this skill's private config path")
    parser.add_argument("--model", default="gpt-image-2")
    parser.add_argument("--quality", default="low")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--output-format", default="png")
    parser.add_argument("--image", action="append", default=[], help="Input image for edit mode; repeatable")
    parser.add_argument("--mask", help="Optional mask image for edit mode")
    parser.add_argument("--debug", action="store_true", help="Print timing and response metadata to stderr")
    args = parser.parse_args()

    if args.show_config_path:
        print(config_path())
        return 0

    if args.clear_api_key:
        removed = clear_config_api_key()
        print("Removed saved image API key." if removed else "No saved image API key found.")
        return 0

    if args.save_api_key:
        path = write_config_api_key(args.save_api_key)
        print(f"Saved image API key to {path}")
        return 0

    if not args.prompt:
        parser.error("--prompt is required unless using --save-api-key, --clear-api-key, or --show-config-path")

    api_key = resolved_api_key(args.api_key)
    if not api_key:
        print("Missing image API key.", file=sys.stderr)
        print('Run: python scripts\\generate_image.py --save-api-key "YOUR_SUBARX_IMAGE_API_KEY"', file=sys.stderr)
        print("This saves only this skill's dedicated image key and does not change OPENAI_API_KEY.", file=sys.stderr)
        return 2

    base = resolved_base_url(args.base_url).rstrip("/")
    out_path = resolved_output_path(args.out, args.output_format)
    debug_log = default_debug_log if args.debug else None

    if args.mode == "generate":
        payload = {
            "model": args.model,
            "prompt": args.prompt,
            "size": args.size,
            "quality": args.quality,
            "n": args.n,
            "output_format": args.output_format,
        }
        result = request_json(f"{base}/v1/images/generations", api_key, payload, debug_log=debug_log)
    else:
        image_paths = [Path(p) for p in args.image]
        if not image_paths:
            print("--mode edit requires at least one --image.", file=sys.stderr)
            return 2
        for path in image_paths:
            if not path.exists():
                print(f"Input image not found: {path}", file=sys.stderr)
                return 2

        fields = {
            "model": args.model,
            "prompt": args.prompt,
            "size": args.size,
            "quality": args.quality,
            "n": str(args.n),
            "output_format": args.output_format,
        }
        files = [("image", path) for path in image_paths]
        if args.mask:
            mask_path = Path(args.mask)
            if not mask_path.exists():
                print(f"Mask not found: {mask_path}", file=sys.stderr)
                return 2
            files.append(("mask", mask_path))
        result = request_multipart(f"{base}/v1/images/edits", api_key, fields, files, debug_log=debug_log)

    save_image(result, out_path, debug_log=debug_log)
    size = out_path.stat().st_size
    print(f"Saved {out_path} ({size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
