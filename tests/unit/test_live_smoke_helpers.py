from __future__ import annotations

import unittest

from tests.smoke import test_live_stack as smoke


class LiveSmokeHelperTests(unittest.TestCase):
    def test_sse_payloads_parse_job_events_and_terminal_state(self) -> None:
        raw = (
            b"id: job_123:queued\n"
            b"retry: 1000\n"
            b"event: job\n"
            b'data: {"id":"job_123","state":"queued"}\n\n'
            b"id: job_123:completed\n"
            b"event: job\n"
            b'data: {"id":"job_123","state":"completed","runtime":"audio-cpu"}\n\n'
        )

        payloads = smoke.sse_payloads(raw)

        self.assertEqual(len(payloads), 2)
        terminal = smoke.terminal_job_event_payload(payloads, "job_123", "completed")
        self.assertIsNotNone(terminal)
        self.assertEqual(terminal["runtime"], "audio-cpu")

    def test_sse_payloads_preserve_non_json_data(self) -> None:
        self.assertEqual(smoke.sse_payloads(b"event: notice\ndata: keepalive\n\n"), ["keepalive"])

    def test_response_header_is_case_insensitive(self) -> None:
        headers = {"Content-Length": "14", "etag": '"artifact"'}

        self.assertEqual(smoke.response_header(headers, "content-length"), "14")
        self.assertEqual(smoke.response_header(headers, "ETag"), '"artifact"')
        self.assertEqual(smoke.response_header(headers, "missing"), "")


if __name__ == "__main__":
    unittest.main()
