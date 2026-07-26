from __future__ import annotations

import hashlib
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

    def test_downloaded_artifact_proof_records_integrity_headers_and_digest(self) -> None:
        content = b"verified tts bytes"
        digest = hashlib.sha256(content).hexdigest()

        proof = smoke.downloaded_artifact_proof(
            {
                "url": "/artifacts/smoke/tts-0.wav",
                "id": "artifact-tts-0",
                "kind": "audio",
                "mime_type": "audio/wav",
                "bytes": len(content),
                "sha256": digest,
            },
            {
                "Content-Type": "audio/wav",
                "Content-Length": str(len(content)),
                "ETag": '"sha256:' + digest + '"',
                "Accept-Ranges": "bytes",
            },
            content,
            index=1,
        )

        self.assertEqual(proof["artifact_index"], 1)
        self.assertEqual(proof["artifact_url"], "/artifacts/smoke/tts-0.wav")
        self.assertEqual(proof["artifact_bytes"], len(content))
        self.assertEqual(proof["artifact_sha256"], digest)
        self.assertEqual(proof["download_bytes"], len(content))
        self.assertEqual(proof["download_sha256"], digest)
        self.assertEqual(proof["download_accept_ranges"], "bytes")

    def test_artifact_collection_proof_keeps_counts_and_first_artifact_fields(self) -> None:
        proofs = [
            {
                "artifact_url": "/artifacts/smoke/tts-0.wav",
                "artifact_id": "a0",
                "artifact_kind": "audio",
                "artifact_mime_type": "audio/wav",
                "artifact_bytes": 10,
                "artifact_sha256": "1" * 64,
                "download_bytes": 10,
                "download_sha256": "1" * 64,
                "download_content_type": "audio/wav",
                "download_content_length": "10",
                "download_etag": '"sha256:' + "1" * 64 + '"',
                "download_accept_ranges": "bytes",
            },
            {
                "artifact_url": "/artifacts/smoke/tts-1.wav",
                "artifact_id": "a1",
                "artifact_kind": "audio",
                "artifact_mime_type": "audio/wav",
                "artifact_bytes": 12,
                "artifact_sha256": "2" * 64,
                "download_bytes": 12,
                "download_sha256": "2" * 64,
                "download_content_type": "audio/wav",
                "download_content_length": "12",
                "download_etag": '"sha256:' + "2" * 64 + '"',
                "download_accept_ranges": "bytes",
            },
        ]

        collection = smoke.artifact_collection_proof("job_smoke_tts_1", proofs)

        self.assertEqual(collection["artifact_count"], 2)
        self.assertEqual(collection["verified_artifact_count"], 2)
        self.assertEqual(collection["total_downloaded_bytes"], 22)
        self.assertEqual(collection["first_artifact_sha256"], "1" * 64)
        self.assertEqual(collection["artifact_proofs"], proofs)


if __name__ == "__main__":
    unittest.main()
