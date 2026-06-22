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


class SaveImageTests(unittest.TestCase):
    def test_save_image_writes_b64_payload(self):
        png_header = b"\x89PNG\r\n\x1a\n"
        result = {"data": [{"b64_json": generate_image.base64.b64encode(png_header).decode("ascii")}]}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.png"
            generate_image.save_image(result, out)
            self.assertEqual(png_header, out.read_bytes())


if __name__ == "__main__":
    unittest.main()
