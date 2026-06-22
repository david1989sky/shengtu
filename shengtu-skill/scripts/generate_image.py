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


DEFAULT_BASE_URL = "https://www.subarx.com"
API_KEY_ENV_NAMES = ("SUBARX_IMAGE_API_KEY", "SUBARX_API_KEY", "AISTATION_API_KEY", "AIWANWU_API_KEY")
CONFIG_FILE_NAME = "config.json"
DEFAULT_OUTPUT_DIR_NAME = "生图"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 180
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


def perform_request(req: urllib.request.Request, *, max_retries: int = DEFAULT_MAX_RETRIES) -> dict:
    last_error: RuntimeError | None = None
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"HTTP {exc.code}: {detail}")
            if attempt >= max_retries or not should_retry_http_error(exc):
                raise last_error from exc
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f"Network error: {exc}")
            if attempt >= max_retries:
                raise last_error from exc

        time.sleep(backoff_seconds(attempt + 1))

    raise last_error or RuntimeError("request failed")


def request_json(url: str, api_key: str, payload: dict) -> dict:
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
    return perform_request(req)


def request_multipart(url: str, api_key: str, fields: dict[str, str], files: list[tuple[str, Path]]) -> dict:
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
    return perform_request(req)


def save_image(result: dict, out_path: Path) -> None:
    data = result.get("data") or []
    if not data:
        raise RuntimeError(f"Response has no data array: {json.dumps(result, ensure_ascii=False)[:1000]}")

    first = data[0]
    b64_json = first.get("b64_json")
    image_url = first.get("url")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if b64_json:
        out_path.write_bytes(base64.b64decode(b64_json))
        return

    if image_url:
        with urllib.request.urlopen(image_url, timeout=180) as resp:
            out_path.write_bytes(resp.read())
        return

    raise RuntimeError(f"Response has neither b64_json nor url: {json.dumps(first, ensure_ascii=False)[:1000]}")


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

    if args.mode == "generate":
        payload = {
            "model": args.model,
            "prompt": args.prompt,
            "size": args.size,
            "quality": args.quality,
            "n": args.n,
            "output_format": args.output_format,
        }
        result = request_json(f"{base}/v1/images/generations", api_key, payload)
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
        result = request_multipart(f"{base}/v1/images/edits", api_key, fields, files)

    save_image(result, out_path)
    size = out_path.stat().st_size
    print(f"Saved {out_path} ({size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
