import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import generate_image


class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code: int, body: str):
        super().__init__(
            url="https://www.subarx.com/v1/images/generations",
            code=code,
            msg="error",
            hdrs=None,
            fp=None,
        )
        self._body = body.encode("utf-8")

    def read(self):
        return self._body


class RequestRetryTests(unittest.TestCase):
    def test_request_json_retries_once_on_504_then_succeeds(self):
        payload = {"ok": True}
        success_response = mock.MagicMock()
        success_response.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
        success_response.__enter__.return_value.status = 200

        with mock.patch.object(
            generate_image.urllib.request,
            "urlopen",
            side_effect=[FakeHTTPError(504, "error code: 504"), success_response],
        ) as urlopen, mock.patch("time.sleep") as sleep:
            result = generate_image.request_json("https://www.subarx.com/v1/images/generations", "k", {"x": 1})

        self.assertEqual(payload, result)
        self.assertEqual(2, urlopen.call_count)
        sleep.assert_called_once()

    def test_request_json_raises_after_retry_budget_exhausted(self):
        with mock.patch.object(
            generate_image.urllib.request,
            "urlopen",
            side_effect=[FakeHTTPError(504, "error code: 504")] * 3,
        ), mock.patch("time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                generate_image.request_json("https://www.subarx.com/v1/images/generations", "k", {"x": 1})

        self.assertIn("HTTP 504", str(ctx.exception))

    def test_request_json_debug_logs_attempts_and_response_metadata(self):
        payload = {"request_id": "req_123", "data": [{"url": "https://example.com/image.png"}]}
        success_response = mock.MagicMock()
        success_response.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
        success_response.__enter__.return_value.status = 200
        success_response.__enter__.return_value.headers = {"x-request-id": "header_req_123"}

        debug = mock.Mock()
        with mock.patch.object(
            generate_image.urllib.request,
            "urlopen",
            return_value=success_response,
        ):
            result = generate_image.request_json(
                "https://www.subarx.com/v1/images/generations",
                "k",
                {"x": 1},
                debug_log=debug,
            )

        self.assertEqual(payload, result)
        joined = "\n".join(call.args[0] for call in debug.call_args_list)
        self.assertIn("attempt 1/3", joined)
        self.assertIn("response status=200", joined)
        self.assertIn("x_request_id=header_req_123", joined)


class DescribeResultTests(unittest.TestCase):
    def test_default_request_timeout_seconds_is_300(self):
        self.assertEqual(300, generate_image.DEFAULT_REQUEST_TIMEOUT_SECONDS)

    def test_resolved_base_url_defaults_to_st_subarx(self):
        self.assertEqual("https://st.subarx.com", generate_image.resolved_base_url())

    def test_describe_result_includes_request_and_payload_shape(self):
        result = {
            "request_id": "req_abc",
            "data": [{"b64_json": "abc"}],
        }

        summary = generate_image.describe_result(result)

        self.assertIn("request_id=req_abc", summary)
        self.assertIn("items=1", summary)
        self.assertIn("first_item=b64_json", summary)

    def test_describe_result_identifies_responses_api_output_shape(self):
        result = {
            "id": "resp_abc",
            "output": [
                {
                    "type": "image_generation_call",
                    "result": {"base64": "abc"},
                }
            ],
        }

        summary = generate_image.describe_result(result)

        self.assertIn("request_id=resp_abc", summary)
        self.assertIn("shape=responses", summary)
        self.assertIn("items=1", summary)
        self.assertIn("first_item=base64", summary)


class SaveImageTests(unittest.TestCase):
    def test_save_image_writes_b64_payload(self):
        png_header = b"\x89PNG\r\n\x1a\n"
        result = {"data": [{"b64_json": generate_image.base64.b64encode(png_header).decode("ascii")}]}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.png"
            generate_image.save_image(result, out)
            self.assertEqual(png_header, out.read_bytes())

    def test_save_image_writes_responses_api_result_base64_payload(self):
        png_header = b"\x89PNG\r\n\x1a\n"
        result = {
            "output": [
                {
                    "type": "image_generation_call",
                    "result": {"base64": generate_image.base64.b64encode(png_header).decode("ascii")},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.png"
            generate_image.save_image(result, out)
            self.assertEqual(png_header, out.read_bytes())

    def test_save_image_writes_data_url_payload(self):
        png_header = b"\x89PNG\r\n\x1a\n"
        encoded = generate_image.base64.b64encode(png_header).decode("ascii")
        result = {"data": [{"url": f"data:image/png;base64,{encoded}"}]}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.png"
            generate_image.save_image(result, out)
            self.assertEqual(png_header, out.read_bytes())

    def test_save_image_retries_downloaded_url_and_logs_source(self):
        png_header = b"\x89PNG\r\n\x1a\n"
        failed_response = mock.MagicMock()
        failed_response.__enter__.side_effect = urllib.error.URLError("temporary")
        success_response = mock.MagicMock()
        success_response.__enter__.return_value.read.return_value = png_header
        result = {"data": [{"url": "https://example.com/image.png"}]}
        debug = mock.Mock()

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.png"
            with mock.patch.object(
                generate_image.urllib.request,
                "urlopen",
                side_effect=[failed_response, success_response],
            ), mock.patch("time.sleep") as sleep:
                generate_image.save_image(result, out, debug_log=debug)

            self.assertEqual(png_header, out.read_bytes())

        sleep.assert_called_once()
        joined = "\n".join(call.args[0] for call in debug.call_args_list)
        self.assertIn("image_source shape=images kind=url", joined)
        self.assertIn("download_error", joined)


class UpscaleTests(unittest.TestCase):
    def test_parse_size_accepts_width_by_height(self):
        self.assertEqual((3840, 2160), generate_image.parse_size("3840x2160"))

    def test_parse_size_rejects_invalid_value(self):
        with self.assertRaises(ValueError):
            generate_image.parse_size("4K")

    def test_read_png_size_reads_header_dimensions(self):
        header = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + (3840).to_bytes(4, "big")
            + (2160).to_bytes(4, "big")
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake.png"
            path.write_bytes(header)
            self.assertEqual((3840, 2160), generate_image.read_image_size(path))

    def test_maybe_upscale_skips_when_image_already_matches_target(self):
        header = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + (3840).to_bytes(4, "big")
            + (2160).to_bytes(4, "big")
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "already-4k.png"
            path.write_bytes(header)
            with mock.patch.object(generate_image, "resize_exact_image") as resize:
                generate_image.maybe_upscale_image(
                    path,
                    enabled=True,
                    target_size="3840x2160",
                    upscaler="auto",
                    realesrgan_bin="realesrgan-ncnn-vulkan",
                    realesrgan_model=generate_image.DEFAULT_REALESRGAN_MODEL,
                )
            resize.assert_not_called()

    def test_auto_upscaler_prefers_realesrgan_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.png"
            out = Path(tmp) / "out.png"
            src.write_bytes(b"not-a-real-image")
            with mock.patch.object(generate_image, "command_exists", return_value="/usr/local/bin/realesrgan-ncnn-vulkan"):
                with mock.patch.object(generate_image, "run_realesrgan") as run_realesrgan:
                    method = generate_image.resize_exact_image(src, out, 3840, 2160, "auto")

            self.assertEqual("realesrgan", method)
            run_realesrgan.assert_called_once()


if __name__ == "__main__":
    unittest.main()
