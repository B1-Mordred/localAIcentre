from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "integrations" / "b1-model-client"))

from b1_model_client import __main__ as client  # noqa: E402


MODELHUB_EVIDENCE_FORMAT = "b1-ai-hub-modelhub-client-sync/v1"
MODELHUB_REQUIRED_CHECKS = (
    "catalog_visible",
    "download_plan_created",
    "head_metadata_validated",
    "etag_if_none_match_validated",
    "range_resume_downloaded",
    "cache_state_managed",
    "dry_run_prune_safe",
    "inference_only_download_blocked",
)


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@unittest.skipUnless(os.getenv("B1_MODELHUB_LIVE_TEST") == "1", "set B1_MODELHUB_LIVE_TEST=1 to run live Model Hub client sync tests")
class ModelHubClientSyncCompatibilityTests(unittest.TestCase):
    checks: dict[str, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []

    @classmethod
    def setUpClass(cls) -> None:
        cls.checks = {}
        cls.samples = []
        cls.base_url = os.getenv("B1_MODELHUB_URL", "https://models.ai.b1.germering").rstrip("/")
        cls.token = os.getenv("B1_MODELHUB_TOKEN", "")
        cls.ca_file = client.resolve_ca_file(argparse.Namespace(ca_file=None))
        cls.sync_model = os.getenv("B1_MODELHUB_SYNC_MODEL", "").strip()
        cls.inference_only_model = os.getenv("B1_MODELHUB_INFERENCE_ONLY_MODEL", "").strip()
        cls.accept_licenses = env_flag("B1_MODELHUB_ACCEPT_LICENSES", False)
        if not cls.token:
            raise unittest.SkipTest("set B1_MODELHUB_TOKEN to a dedicated Model Hub client or admin API key")
        if not cls.sync_model:
            raise unittest.SkipTest("set B1_MODELHUB_SYNC_MODEL to a permitted downloadable model or alias")
        if not cls.inference_only_model:
            raise unittest.SkipTest("set B1_MODELHUB_INFERENCE_ONLY_MODEL to an inference-only model or alias")

    @classmethod
    def tearDownClass(cls) -> None:
        evidence_path = os.getenv("B1_MODELHUB_EVIDENCE", "").strip()
        if not evidence_path:
            return
        path = Path(evidence_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        status = "ok" if all(cls.checks.get(name, {}).get("status") == "ok" for name in MODELHUB_REQUIRED_CHECKS) else "incomplete"
        path.write_text(
            json.dumps(
                {
                    "format": MODELHUB_EVIDENCE_FORMAT,
                    "generated_at": datetime.now(tz=UTC).isoformat(),
                    "base_url": cls.base_url,
                    "status": status,
                    "required_checks": list(MODELHUB_REQUIRED_CHECKS),
                    "checks": cls.checks,
                    "samples": cls.samples,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def record_check(self, name: str, status: str = "ok", **data: Any) -> None:
        self.checks[name] = {
            "status": status,
            "recorded_at": datetime.now(tz=UTC).isoformat(),
            **data,
        }

    def range_seed_partial(self, cache: Path, action: dict[str, Any]) -> int:
        blob = str(action["blob"]).lower()
        expected_size = int(action["expected_size"])
        configured_partial_size = int(os.getenv("B1_MODELHUB_PARTIAL_BYTES", "1048576"))
        partial_size = 1 if expected_size <= 1 else max(1, min(expected_size - 1, configured_partial_size))
        request = urllib.request.Request(
            client.modelhub_request_url(self.base_url, f"/modelhub/v1/blobs/{blob}", self.token)
        )
        request.add_header("Accept", "application/octet-stream")
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Range", f"bytes=0-{partial_size - 1}")
        if self.accept_licenses:
            accepted_refs = client.accepted_license_refs_for_action(action)
            if accepted_refs:
                request.add_header("X-B1-Accept-License", ", ".join(sorted(accepted_refs)))
        with client.modelhub_urlopen(request, timeout=120, ca_file=self.ca_file) as response:
            status = getattr(response, "status", response.getcode())
            self.assertEqual(status, 206, "Model Hub blob downloads must support HTTP Range resume")
            content = response.read()
        self.assertEqual(len(content), partial_size)
        partial = cache / "blobs" / f"{blob}.partial"
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(content)
        return partial_size

    def cache_blob_evidence(self, cache: Path, blob: str, expected_size: int) -> dict[str, Any]:
        target = cache / "blobs" / blob
        partial = target.with_suffix(".partial")
        cache_root = cache.resolve()
        blob_path = target.resolve(strict=True)
        relative_path = ""
        path_within_cache_root = False
        try:
            relative_path = blob_path.relative_to(cache_root).as_posix()
            path_within_cache_root = True
        except ValueError:
            pass
        target_stat = target.stat()
        evidence: dict[str, Any] = {
            "cache_root": str(cache_root),
            "cached_blob_relative_path": relative_path,
            "path_within_cache_root": path_within_cache_root,
            "cached_blob_size": target_stat.st_size,
            "cached_blob_file_sha256": client.sha256_file(target),
            "cached_blob_is_regular_file": client.is_regular_file_no_symlink(target),
            "cached_blob_is_symlink": target.is_symlink(),
            "partial_removed": not partial.exists() and not partial.is_symlink(),
            "expected_size_matches_file": target_stat.st_size == expected_size,
            "expected_sha256_matches_file": client.sha256_file(target) == blob,
            "posix_mode_checked": os.name != "nt",
        }
        if os.name != "nt":
            cache_mode = cache.stat().st_mode & 0o777
            blobs_mode = target.parent.stat().st_mode & 0o777
            target_mode = target_stat.st_mode & 0o777
            state_file = client.state_path(cache)
            state_mode = state_file.stat().st_mode & 0o777 if state_file.exists() else 0
            evidence.update(
                {
                    "cache_root_mode": oct(cache_mode),
                    "blob_dir_mode": oct(blobs_mode),
                    "cached_blob_mode": oct(target_mode),
                    "state_file_mode": oct(state_mode),
                    "cache_root_private": cache_mode == client.PRIVATE_DIR_MODE,
                    "blob_dir_private": blobs_mode == client.PRIVATE_DIR_MODE,
                    "cached_blob_private": target_mode == client.PRIVATE_FILE_MODE,
                    "state_file_private": state_mode == client.PRIVATE_FILE_MODE,
                }
            )
        return evidence

    def validate_blob_head_metadata(self, action: dict[str, Any]) -> None:
        blob = str(action["blob"]).lower()
        expected_size = int(action["expected_size"])
        request = urllib.request.Request(
            client.modelhub_request_url(self.base_url, f"/modelhub/v1/blobs/{blob}", self.token),
            method="HEAD",
        )
        request.add_header("Accept", "application/octet-stream")
        request.add_header("Authorization", f"Bearer {self.token}")
        if self.accept_licenses:
            accepted_refs = client.accepted_license_refs_for_action(action)
            if accepted_refs:
                request.add_header("X-B1-Accept-License", ", ".join(sorted(accepted_refs)))
        with client.modelhub_urlopen(request, timeout=120, ca_file=self.ca_file) as response:
            status = getattr(response, "status", response.getcode())
            headers = getattr(response, "headers", {})
            self.assertEqual(status, 200, "Model Hub blob HEAD must return metadata for downloadable blobs")
            client.validate_blob_response_headers(blob, expected_size, status, headers, resume_from=0)
            content_length = client.header_value(headers, "Content-Length")
            etag = client.header_value(headers, "ETag")
            checksum = client.header_value(headers, "X-Checksum-SHA256")
            accept_ranges = client.header_value(headers, "Accept-Ranges")
        self.assertEqual(etag, client.expected_etag(blob))
        self.assertEqual(checksum.lower(), blob)
        self.assertEqual(int(content_length), expected_size)
        if accept_ranges:
            self.assertEqual(accept_ranges.lower(), "bytes")
        self.record_check(
            "head_metadata_validated",
            model=self.sync_model,
            blob=blob,
            expected_size=expected_size,
            etag=etag,
            checksum=checksum,
            accept_ranges=accept_ranges,
        )

    def validate_blob_conditional_get(self, action: dict[str, Any]) -> None:
        blob = str(action["blob"]).lower()
        expected_etag = client.expected_etag(blob)
        request = urllib.request.Request(
            client.modelhub_request_url(self.base_url, f"/modelhub/v1/blobs/{blob}", self.token)
        )
        request.add_header("Accept", "application/octet-stream")
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("If-None-Match", expected_etag)
        if self.accept_licenses:
            accepted_refs = client.accepted_license_refs_for_action(action)
            if accepted_refs:
                request.add_header("X-B1-Accept-License", ", ".join(sorted(accepted_refs)))
        try:
            with client.modelhub_urlopen(request, timeout=120, ca_file=self.ca_file) as response:
                status = getattr(response, "status", response.getcode())
                headers = getattr(response, "headers", {})
                body = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            headers = getattr(exc, "headers", {})
            body = exc.read()
            close = getattr(exc, "close", None)
            if callable(close):
                close()
        self.assertEqual(status, 304, "Model Hub blob GET with matching If-None-Match must return HTTP 304")
        self.assertEqual(body, b"", "Model Hub 304 responses must not include blob bytes")
        etag = client.header_value(headers, "ETag")
        checksum = client.header_value(headers, "X-Checksum-SHA256")
        self.assertEqual(etag, expected_etag)
        if checksum:
            self.assertEqual(checksum.lower(), blob)
        self.record_check(
            "etag_if_none_match_validated",
            model=self.sync_model,
            blob=blob,
            etag=etag,
            checksum=checksum,
        )

    def first_download_action(self, cache: Path) -> dict[str, Any]:
        actions = client.planned_actions(self.base_url, self.token, cache, [self.sync_model], ca_file=self.ca_file)
        candidates = [action for action in actions if action.get("action") in {"download", "replace"} and action.get("blob")]
        self.assertTrue(candidates, f"{self.sync_model} did not produce a downloadable Model Hub sync action: {actions}")
        action = candidates[0]
        self.assertTrue(client.is_sha256(str(action["blob"]).lower()))
        self.assertGreater(int(action.get("expected_size") or 0), 0)
        self.record_check(
            "download_plan_created",
            model=self.sync_model,
            blob=str(action["blob"]).lower(),
            expected_size=int(action["expected_size"]),
            requires_license_acceptance=client.action_requires_license_acceptance(action),
        )
        if client.action_requires_license_acceptance(action) and not self.accept_licenses:
            self.skipTest("selected sync model requires licence acceptance; set B1_MODELHUB_ACCEPT_LICENSES=1 after reviewing the plan")
        return action

    def test_model_client_downloads_resumes_verifies_and_blocks_inference_only(self) -> None:
        catalog = client.request_json(self.base_url, "/modelhub/v1/catalog", self.token, ca_file=self.ca_file)
        self.assertTrue(catalog.get("aliases") or catalog.get("models"), "Model Hub catalog is empty or unavailable")
        self.record_check(
            "catalog_visible",
            alias_count=len(catalog.get("aliases") or []),
            model_count=len(catalog.get("models") or []),
        )

        with tempfile.TemporaryDirectory(prefix="b1-modelhub-compat-") as tmp:
            cache = Path(tmp)
            action = self.first_download_action(cache)
            self.validate_blob_head_metadata(action)
            self.validate_blob_conditional_get(action)
            partial_size = self.range_seed_partial(cache, action)
            result = client.sync_once(
                self.base_url,
                self.token,
                cache,
                [self.sync_model],
                accept_licenses=self.accept_licenses,
                ca_file=self.ca_file,
            )
            result_by_blob = {str(item.get("blob", "")).lower(): item for item in result.get("changes", []) if isinstance(item, dict)}
            blob = str(action["blob"]).lower()
            downloaded = result_by_blob.get(blob)
            self.assertIsInstance(downloaded, dict)
            self.assertIn(downloaded.get("status"), {"downloaded", "published", "kept"})
            target = cache / "blobs" / blob
            self.assertTrue(target.is_file())
            self.assertFalse(target.with_suffix(".partial").exists())
            file_evidence = self.cache_blob_evidence(cache, blob, int(action["expected_size"]))
            self.assertEqual(file_evidence["cached_blob_file_sha256"], blob)
            self.assertTrue(file_evidence["path_within_cache_root"])
            self.assertEqual(file_evidence["cached_blob_relative_path"], f"blobs/{blob}")
            self.assertTrue(file_evidence["cached_blob_is_regular_file"])
            self.assertFalse(file_evidence["cached_blob_is_symlink"])
            self.assertTrue(file_evidence["partial_removed"])
            self.record_check(
                "range_resume_downloaded",
                model=self.sync_model,
                blob=blob,
                expected_size=int(action["expected_size"]),
                partial_size=partial_size,
                final_size=target.stat().st_size,
                final_sha256=file_evidence["cached_blob_file_sha256"],
                partial_removed=file_evidence["partial_removed"],
                cached_blob_relative_path=file_evidence["cached_blob_relative_path"],
                path_within_cache_root=file_evidence["path_within_cache_root"],
                cached_blob_is_regular_file=file_evidence["cached_blob_is_regular_file"],
                cached_blob_is_symlink=file_evidence["cached_blob_is_symlink"],
            )

            state = client.load_state(cache)
            self.assertIn(blob, state["managed_blobs"])
            managed_entry = state["managed_blobs"][blob]
            self.assertEqual(managed_entry.get("sha256"), blob)
            self.assertEqual(int(managed_entry.get("size_bytes") or 0), int(action["expected_size"]))
            self.record_check(
                "cache_state_managed",
                model=self.sync_model,
                managed_blob_count=len(state["managed_blobs"]),
                managed_blob_sha256=blob,
                managed_blob_size=int(action["expected_size"]),
                managed_entry_sha256=str(managed_entry.get("sha256") or ""),
                managed_entry_size=int(managed_entry.get("size_bytes") or 0),
                **file_evidence,
            )

            unmanaged = cache / "blobs" / "operator-unmanaged-file"
            unmanaged.write_text("do not prune", encoding="utf-8")
            prune = client.prune_plan(self.base_url, self.token, cache, [self.sync_model], ca_file=self.ca_file)
            self.assertTrue(prune["unmanaged_files_ignored"])
            self.assertNotIn(str(unmanaged), [item.get("path") for item in prune["candidates"]])
            self.record_check(
                "dry_run_prune_safe",
                unmanaged_files_ignored=bool(prune["unmanaged_files_ignored"]),
                candidate_count=len(prune["candidates"]),
            )

        with tempfile.TemporaryDirectory(prefix="b1-modelhub-policy-") as policy_tmp:
            inference_actions = client.planned_actions(
                self.base_url,
                self.token,
                Path(policy_tmp),
                [self.inference_only_model],
                ca_file=self.ca_file,
            )
        blocked = [
            action
            for action in inference_actions
            if action.get("action") == "skip" and "not downloadable" in str(action.get("reason") or "")
        ]
        self.assertTrue(blocked, f"{self.inference_only_model} did not produce a non-downloadable policy skip: {inference_actions}")
        self.record_check("inference_only_download_blocked", model=self.inference_only_model, action_count=len(inference_actions))
        self.samples.append(
            {
                "label": "modelhub-client-sync",
                "sync_model": self.sync_model,
                "inference_only_model": self.inference_only_model,
                "accept_licenses": self.accept_licenses,
                "synced_blob": str(action["blob"]).lower(),
                "synced_size": int(action["expected_size"]),
                "cached_blob_file_sha256": file_evidence["cached_blob_file_sha256"],
                "cached_blob_size": file_evidence["cached_blob_size"],
                "cached_blob_relative_path": file_evidence["cached_blob_relative_path"],
                "path_within_cache_root": file_evidence["path_within_cache_root"],
                "partial_removed": file_evidence["partial_removed"],
            }
        )


if __name__ == "__main__":
    unittest.main()
