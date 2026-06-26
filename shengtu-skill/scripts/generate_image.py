#!/usr/bin/env python3
"""Generate or edit images through the Subarx image gateway API."""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import mimetypes
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NamedTuple
import zipfile


DEFAULT_BASE_URL = "https://st.subarx.com"
API_KEY_ENV_NAMES = ("SUBARX_IMAGE_API_KEY", "SUBARX_API_KEY", "AISTATION_API_KEY", "AIWANWU_API_KEY")
CONFIG_FILE_NAME = "config.json"
DEFAULT_OUTPUT_DIR_NAME = "生图"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 300
DEFAULT_IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 180
DEFAULT_MAX_RETRIES = 2
RETRYABLE_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}
DEFAULT_REALESRGAN_MODEL = "realesrgan-x4plus"
REALESRGAN_RELEASE_BASE_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0"


class RealESRGANPackage(NamedTuple):
    url: str
    binary_name: str


def config_dir() -> Path:
    if os.name == "nt":
        root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(root) / "Codex" / "skills" / "shengtu-skill"
    root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root) / "codex" / "skills" / "shengtu-skill"


def config_path() -> Path:
    return config_dir() / CONFIG_FILE_NAME


def upscaler_tools_dir() -> Path:
    return config_dir() / "tools"


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


def parse_size(value: str) -> tuple[int, int]:
    raw = str(value or "").strip().lower()
    if "x" not in raw:
        raise ValueError(f"invalid size {value!r}; expected WIDTHxHEIGHT")
    left, right = raw.split("x", 1)
    width = int(left.strip())
    height = int(right.strip())
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid size {value!r}; dimensions must be positive")
    return width, height


def read_png_size(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as fh:
            header = fh.read(24)
    except OSError:
        return None
    if len(header) >= 24 and header[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
    return None


def read_jpeg_size(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        i += 2
        if marker in (0xD8, 0xD9):
            continue
        if i + 2 > len(data):
            return None
        segment_len = int.from_bytes(data[i : i + 2], "big")
        if segment_len < 2 or i + segment_len > len(data):
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if segment_len >= 7:
                height = int.from_bytes(data[i + 3 : i + 5], "big")
                width = int.from_bytes(data[i + 5 : i + 7], "big")
                return width, height
        i += segment_len
    return None


def read_image_size(path: Path) -> tuple[int, int] | None:
    return read_png_size(path) or read_jpeg_size(path)


def command_exists(name: str) -> str | None:
    return shutil.which(name)


def platform_realesrgan_package() -> RealESRGANPackage:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        return RealESRGANPackage(
            url=f"{REALESRGAN_RELEASE_BASE_URL}/realesrgan-ncnn-vulkan-20220424-macos.zip",
            binary_name="realesrgan-ncnn-vulkan",
        )
    if system == "windows":
        return RealESRGANPackage(
            url=f"{REALESRGAN_RELEASE_BASE_URL}/realesrgan-ncnn-vulkan-20220424-windows.zip",
            binary_name="realesrgan-ncnn-vulkan.exe",
        )
    if system == "linux" and ("x86_64" in machine or "amd64" in machine):
        return RealESRGANPackage(
            url=f"{REALESRGAN_RELEASE_BASE_URL}/realesrgan-ncnn-vulkan-20220424-ubuntu.zip",
            binary_name="realesrgan-ncnn-vulkan",
        )
    raise RuntimeError(f"Automatic Real-ESRGAN install is not supported on {platform.system()} {platform.machine()}")


def iter_private_realesrgan_binaries(binary_name: str):
    root = upscaler_tools_dir()
    if not root.exists():
        return
    yield from root.rglob(binary_name)


def resolve_realesrgan_binary(binary: str) -> str | None:
    value = str(binary or "").strip()
    if not value:
        value = "realesrgan-ncnn-vulkan.exe" if os.name == "nt" else "realesrgan-ncnn-vulkan"
    explicit = Path(value)
    if explicit.exists():
        return str(explicit)
    found = command_exists(value)
    if found:
        return found
    binary_name = explicit.name
    for candidate in iter_private_realesrgan_binaries(binary_name):
        if candidate.is_file():
            return str(candidate)
    if os.name == "nt" and not binary_name.endswith(".exe"):
        for candidate in iter_private_realesrgan_binaries(binary_name + ".exe"):
            if candidate.is_file():
                return str(candidate)
    return None


def download_file_with_retries(url: str, out_path: Path, *, max_retries: int = DEFAULT_MAX_RETRIES, debug_log=None) -> None:
    debug = debug_log or (lambda _message: None)
    last_error: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            debug(f"download attempt {attempt + 1}/{max_retries + 1} url={url}")
            req = urllib.request.Request(url, headers={"User-Agent": "shengtu-skill/1.0"})
            with urllib.request.urlopen(req, timeout=DEFAULT_IMAGE_DOWNLOAD_TIMEOUT_SECONDS) as resp:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with out_path.open("wb") as fh:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
            return
        except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.RemoteDisconnected) as exc:
            last_error = exc
            try:
                out_path.unlink()
            except FileNotFoundError:
                pass
            debug(f"download_error attempt={attempt + 1} error={exc}")
            if attempt >= max_retries:
                raise RuntimeError(f"Download failed after {max_retries + 1} attempts: {url}") from exc
            time.sleep(backoff_seconds(attempt + 1))
    raise RuntimeError(f"Download failed: {url}") from last_error


def install_realesrgan(*, install_root: Path | None = None, debug_log=None) -> Path:
    package = platform_realesrgan_package()
    root = install_root or upscaler_tools_dir()
    root.mkdir(parents=True, exist_ok=True)
    debug = debug_log or (lambda _message: None)
    with TemporaryDirectory() as tmp:
        archive_path = Path(tmp) / "realesrgan.zip"
        download_file_with_retries(package.url, archive_path, debug_log=debug_log)
        extract_dir = root / "realesrgan-ncnn-vulkan"
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(extract_dir)
    candidates = list(extract_dir.rglob(package.binary_name))
    if not candidates:
        raise RuntimeError(f"Installed package did not contain {package.binary_name}")
    binary = candidates[0]
    if os.name != "nt":
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary


def run_command(args: list[str], *, debug_log=None) -> None:
    debug = debug_log or (lambda _message: None)
    debug("run " + " ".join(args))
    completed = subprocess.run(args, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"Command failed ({completed.returncode}): {' '.join(args)}\n{details}")


def resize_exact_with_pillow(input_path: Path, out_path: Path, target_width: int, target_height: int, *, debug_log=None) -> None:
    try:
        from PIL import Image, ImageFilter
    except ImportError as exc:
        raise RuntimeError("Pillow is required for pillow upscaling. Install with: python -m pip install pillow") from exc

    debug = debug_log or (lambda _message: None)
    with Image.open(input_path) as image:
        image = image.convert("RGB")
        src_width, src_height = image.size
        target_ratio = target_width / target_height
        src_ratio = src_width / src_height
        if src_ratio > target_ratio:
            crop_width = int(round(src_height * target_ratio))
            left = max(0, (src_width - crop_width) // 2)
            image = image.crop((left, 0, left + crop_width, src_height))
        elif src_ratio < target_ratio:
            crop_height = int(round(src_width / target_ratio))
            top = max(0, (src_height - crop_height) // 2)
            image = image.crop((0, top, src_width, top + crop_height))
        image = image.resize((target_width, target_height), Image.Resampling.LANCZOS)
        image = image.filter(ImageFilter.UnsharpMask(radius=1.1, percent=110, threshold=3))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(out_path)
        debug(f"upscale_pillow source={src_width}x{src_height} target={target_width}x{target_height}")


def resize_exact_with_sips(input_path: Path, out_path: Path, target_width: int, target_height: int, *, debug_log=None) -> None:
    if not command_exists("sips"):
        raise RuntimeError("sips is not available on this system")
    size = read_image_size(input_path)
    if not size:
        raise RuntimeError(f"Cannot determine image size: {input_path}")
    src_width, src_height = size
    target_ratio = target_width / target_height
    src_ratio = src_width / src_height
    crop_width = src_width
    crop_height = src_height
    if src_ratio > target_ratio:
        crop_width = int(round(src_height * target_ratio))
    elif src_ratio < target_ratio:
        crop_height = int(round(src_width / target_ratio))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as tmp:
        cropped = Path(tmp) / f"crop{input_path.suffix or '.png'}"
        run_command(
            [
                "sips",
                "-c",
                str(crop_height),
                str(crop_width),
                str(input_path),
                "--out",
                str(cropped),
            ],
            debug_log=debug_log,
        )
        run_command(
            [
                "sips",
                "-z",
                str(target_height),
                str(target_width),
                str(cropped),
                "--out",
                str(out_path),
            ],
            debug_log=debug_log,
        )


def run_realesrgan(
    input_path: Path,
    out_path: Path,
    *,
    target_width: int,
    target_height: int,
    binary: str,
    model: str,
    debug_log=None,
) -> None:
    resolved_binary = resolve_realesrgan_binary(binary)
    if not resolved_binary:
        raise RuntimeError(
            f"{binary} is not installed. Install Real-ESRGAN ncnn Vulkan and make realesrgan-ncnn-vulkan available in PATH."
        )
    size = read_image_size(input_path)
    scale = 4
    if size:
        src_width, src_height = size
        scale = max(2, min(4, int(max(target_width / src_width, target_height / src_height) + 0.999)))
    with TemporaryDirectory() as tmp:
        ai_out = Path(tmp) / f"realesrgan{input_path.suffix or '.png'}"
        run_command(
            [
                resolved_binary,
                "-i",
                str(input_path),
                "-o",
                str(ai_out),
                "-n",
                model,
                "-s",
                str(scale),
            ],
            debug_log=debug_log,
        )
        resize_exact_image(ai_out, out_path, target_width, target_height, "auto-finalize", debug_log=debug_log)


def resize_exact_image(
    input_path: Path,
    out_path: Path,
    target_width: int,
    target_height: int,
    upscaler: str,
    *,
    realesrgan_bin: str = "realesrgan-ncnn-vulkan",
    realesrgan_model: str = DEFAULT_REALESRGAN_MODEL,
    debug_log=None,
) -> str:
    if upscaler == "realesrgan":
        run_realesrgan(
            input_path,
            out_path,
            target_width=target_width,
            target_height=target_height,
            binary=realesrgan_bin,
            model=realesrgan_model,
            debug_log=debug_log,
        )
        return "realesrgan"
    if upscaler == "sips":
        resize_exact_with_sips(input_path, out_path, target_width, target_height, debug_log=debug_log)
        return "sips"
    if upscaler == "pillow":
        resize_exact_with_pillow(input_path, out_path, target_width, target_height, debug_log=debug_log)
        return "pillow"
    if upscaler == "auto-finalize":
        try:
            resize_exact_with_pillow(input_path, out_path, target_width, target_height, debug_log=debug_log)
            return "pillow"
        except RuntimeError:
            resize_exact_with_sips(input_path, out_path, target_width, target_height, debug_log=debug_log)
            return "sips"
    if upscaler != "auto":
        raise ValueError(f"unsupported upscaler: {upscaler}")
    if resolve_realesrgan_binary(realesrgan_bin):
        try:
            return resize_exact_image(
                input_path,
                out_path,
                target_width,
                target_height,
                "realesrgan",
                realesrgan_bin=realesrgan_bin,
                realesrgan_model=realesrgan_model,
                debug_log=debug_log,
            )
        except RuntimeError as exc:
            if debug_log:
                debug_log(f"realesrgan_failed_fallback error={exc}")
    try:
        return resize_exact_image(input_path, out_path, target_width, target_height, "pillow", debug_log=debug_log)
    except RuntimeError:
        return resize_exact_image(input_path, out_path, target_width, target_height, "sips", debug_log=debug_log)


def maybe_upscale_image(
    out_path: Path,
    *,
    enabled: bool,
    target_size: str,
    upscaler: str,
    realesrgan_bin: str,
    realesrgan_model: str,
    ensure_upscaler: bool = False,
    debug_log=None,
) -> None:
    if not enabled:
        return
    if ensure_upscaler and upscaler in ("auto", "realesrgan") and not resolve_realesrgan_binary(realesrgan_bin):
        binary = install_realesrgan(debug_log=debug_log)
        realesrgan_bin = str(binary)
    target_width, target_height = parse_size(target_size)
    current = read_image_size(out_path)
    if current == (target_width, target_height):
        if debug_log:
            debug_log(f"upscale_skip already_target_size={target_width}x{target_height}")
        return
    tmp_out = out_path.with_name(f"{out_path.stem}.upscaled{out_path.suffix or '.png'}")
    method = resize_exact_image(
        out_path,
        tmp_out,
        target_width,
        target_height,
        upscaler,
        realesrgan_bin=realesrgan_bin,
        realesrgan_model=realesrgan_model,
        debug_log=debug_log,
    )
    tmp_out.replace(out_path)
    final_size = read_image_size(out_path)
    if final_size != (target_width, target_height):
        raise RuntimeError(f"Upscale failed: expected {target_width}x{target_height}, got {final_size}")
    print(f"Upscaled {out_path} to {target_width}x{target_height} via {method}")


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
    parser.add_argument("--install-upscaler", action="store_true", help="Download Real-ESRGAN ncnn Vulkan into this skill's private tools directory and exit")
    parser.add_argument("--model", default="gpt-image-2")
    parser.add_argument("--quality", default="low")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--output-format", default="png")
    parser.add_argument("--image", action="append", default=[], help="Input image for edit mode; repeatable")
    parser.add_argument("--mask", help="Optional mask image for edit mode")
    parser.add_argument("--upscale", action="store_true", help="Post-process the saved image to --target-size")
    parser.add_argument("--target-size", default="", help="Final WIDTHxHEIGHT after --upscale, for example 3840x2160")
    parser.add_argument(
        "--upscaler",
        choices=("auto", "realesrgan", "sips", "pillow"),
        default="auto",
        help="Upscaler backend. realesrgan is AI; sips/pillow are non-AI resize fallbacks.",
    )
    parser.add_argument("--realesrgan-bin", default="realesrgan-ncnn-vulkan")
    parser.add_argument("--realesrgan-model", default=DEFAULT_REALESRGAN_MODEL)
    parser.add_argument("--ensure-upscaler", action="store_true", help="Install Real-ESRGAN automatically before upscaling if missing")
    parser.add_argument("--debug", action="store_true", help="Print timing and response metadata to stderr")
    args = parser.parse_args()

    if args.show_config_path:
        print(config_path())
        return 0

    if args.clear_api_key:
        removed = clear_config_api_key()
        print("Removed saved image API key." if removed else "No saved image API key found.")
        return 0

    debug_log = default_debug_log if args.debug else None

    if args.install_upscaler:
        binary = install_realesrgan(debug_log=debug_log)
        print(f"Installed Real-ESRGAN upscaler: {binary}")
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
    if args.upscale:
        maybe_upscale_image(
            out_path,
            enabled=True,
            target_size=args.target_size or args.size,
            upscaler=args.upscaler,
            realesrgan_bin=args.realesrgan_bin,
            realesrgan_model=args.realesrgan_model,
            ensure_upscaler=args.ensure_upscaler,
            debug_log=debug_log,
        )
    size = out_path.stat().st_size
    print(f"Saved {out_path} ({size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
