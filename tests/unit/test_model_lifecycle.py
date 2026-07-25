from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
import tarfile
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from app import model_lifecycle, security  # noqa: E402
from app.catalog import parse_manifest_payload  # noqa: E402
from app.scheduler import ResourcePolicy  # noqa: E402


def manifest_payload(sha256: str, size: int, source_url: str = "https://models.ai.b1.germering/test", source_type: str = "catalog") -> dict:
    return {
        "id": "chat-small",
        "version": "1.0.0",
        "display_name": "Chat Small",
        "modality": "llm",
        "operations": ["chat"],
        "source": {"type": source_type, "url": source_url, "revision": "1.0.0"},
        "files": [{"path": "chat-small.gguf", "sha256": sha256, "size_bytes": size}],
        "runtimes": ["localai"],
        "preferred_runtime": "localai",
        "resource_estimate": {"vram_gib": 4, "ram_gib": 4, "disk_gib": 1},
        "license": {"name": "test", "redistribution": "downloadable"},
        "execution_modes": ["hosted-inference", "downloadable"],
        "aliases": ["chat-default"],
    }


class ModelLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        original_resolver = security.resolve_hostname_addresses
        security.resolve_hostname_addresses = lambda hostname, port: ["93.184.216.34"]
        self.addCleanup(lambda: setattr(security, "resolve_hostname_addresses", original_resolver))

    def test_safe_zip_archive_extracts_into_runtime_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bundle.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("weights/model.gguf", b"tiny model")
                handle.writestr("tokenizer.json", b'{"model":"tiny"}')
            archive_bytes = archive.read_bytes()
            digest = hashlib.sha256(archive_bytes).hexdigest()
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            (blob_dir / digest).write_bytes(archive_bytes)
            payload = manifest_payload(digest, len(archive_bytes))
            payload["files"] = [{"path": "bundle.zip", "sha256": digest, "size_bytes": len(archive_bytes), "format": "zip"}]
            manifest = parse_manifest_payload(payload)

            plan = model_lifecycle.build_install_plan(manifest, root, ResourcePolicy(), known_aliases={"chat-default"})
            self.assertTrue(plan["can_install"])
            self.assertEqual(plan["archive_inspections"][0]["inspection_status"], "safe")

            views = model_lifecycle.create_runtime_views(manifest, root)

            view_root = root / "models" / "runtime-views" / "localai" / "chat-small" / "1.0.0"
            self.assertEqual(views[0]["files"][0]["link_type"], "safe-archive-extract")
            self.assertFalse((view_root / "bundle.zip").exists())
            self.assertEqual((view_root / "weights" / "model.gguf").read_bytes(), b"tiny model")
            self.assertEqual((view_root / "tokenizer.json").read_text(encoding="utf-8"), '{"model":"tiny"}')
            view_manifest = json.loads((view_root / "manifest.b1.json").read_text(encoding="utf-8"))
            self.assertNotIn(".staging", json.dumps(view_manifest))
            self.assertEqual(view_manifest["files"][0]["view_path"], str(view_root))
            self.assertEqual(view_manifest["files"][0]["extracted_files"][0]["destination"], str(view_root / "weights" / "model.gguf"))

    def test_runtime_view_creation_cleans_staged_archive_failure(self) -> None:
        data = b"archive bytes"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            (blob_dir / digest).write_bytes(data)
            payload = manifest_payload(digest, len(data))
            payload["files"] = [{"path": "bundle.zip", "sha256": digest, "size_bytes": len(data), "format": "zip"}]
            manifest = parse_manifest_payload(payload)
            original_extract = model_lifecycle.safe_extract_archive

            def failing_extract(_archive_path: Path, target_dir: Path, **_kwargs: object) -> dict:
                target_dir.mkdir(parents=True, exist_ok=True)
                (target_dir / "partial.gguf").write_bytes(b"partial")
                raise model_lifecycle.ModelLifecycleError("simulated extraction failure")

            model_lifecycle.safe_extract_archive = failing_extract
            try:
                with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, "simulated extraction failure"):
                    model_lifecycle.create_runtime_views(manifest, root)
            finally:
                model_lifecycle.safe_extract_archive = original_extract

            view_root = root / "models" / "runtime-views" / "localai" / "chat-small" / "1.0.0"
            staging_root = root / "models" / "runtime-views" / ".staging"
            self.assertFalse(view_root.exists())
            self.assertFalse(staging_root.exists() and any(staging_root.rglob("*")))

    def test_runtime_view_creation_publishes_no_runtime_until_all_stage(self) -> None:
        data = b"runtime view model"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            (blob_dir / digest).write_bytes(data)
            manifest = parse_manifest_payload(
                {
                    **manifest_payload(digest, len(data)),
                    "runtimes": ["localai", "comfyui"],
                    "preferred_runtime": "localai",
                }
            )
            original_hardlink = model_lifecycle.hardlink_blob_into_view

            def failing_hardlink(target: Path, link_path: Path, view_root: Path) -> dict:
                if "comfyui" in view_root.parts:
                    raise model_lifecycle.ModelLifecycleError("simulated second-runtime failure")
                return original_hardlink(target, link_path, view_root)

            model_lifecycle.hardlink_blob_into_view = failing_hardlink
            try:
                with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, "second-runtime failure"):
                    model_lifecycle.create_runtime_views(manifest, root)
            finally:
                model_lifecycle.hardlink_blob_into_view = original_hardlink

            self.assertFalse((root / "models" / "runtime-views" / "localai" / "chat-small" / "1.0.0").exists())
            self.assertFalse((root / "models" / "runtime-views" / "comfyui" / "chat-small" / "1.0.0").exists())
            staging_root = root / "models" / "runtime-views" / ".staging"
            self.assertFalse(staging_root.exists() and any(staging_root.rglob("*")))

    def test_safe_zip_archive_rejects_traversal(self) -> None:
        for member_name in (
            "../escape.gguf",
            "%2e%2e/escape.gguf",
            "weights/safe%2Fescape.gguf",
            "weights/%/escape.gguf",
            "weights/%2/escape.gguf",
            "weights/%zz/escape.gguf",
            "weights/%ffescape.gguf",
        ):
            with self.subTest(member_name=member_name), tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "bad.zip"
                with zipfile.ZipFile(archive, "w") as handle:
                    handle.writestr(member_name, b"nope")

                with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, "unsafe"):
                    model_lifecycle.inspect_archive(archive)

    def test_safe_zip_archive_rejects_symlink_members(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "bad.zip"
            info = zipfile.ZipInfo("weights/link.gguf")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr(info, "target.gguf")

            with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, "symlink"):
                model_lifecycle.inspect_archive(archive)

    def test_safe_tar_archive_rejects_links_devices_and_traversal(self) -> None:
        cases = [
            ("link.tar", tarfile.SYMTYPE, "weights/link.gguf", "not a regular file"),
            ("device.tar", tarfile.CHRTYPE, "weights/device", "not a regular file"),
            ("traversal.tar", tarfile.REGTYPE, "../../escape.gguf", "unsafe relative path"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename, member_type, member_name, expected in cases:
                archive = root / filename
                with tarfile.open(archive, "w") as handle:
                    info = tarfile.TarInfo(member_name)
                    info.type = member_type
                    if member_type == tarfile.REGTYPE:
                        data = b"bad"
                        info.size = len(data)
                        handle.addfile(info, io.BytesIO(data))
                    else:
                        info.linkname = "weights/model.gguf"
                        handle.addfile(info)

                with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, expected):
                    model_lifecycle.inspect_archive(archive)

    def test_archive_limits_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "limited.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("one.gguf", b"1234")
                handle.writestr("two.json", b"56")

            with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, "too many members"):
                model_lifecycle.inspect_archive(archive, max_members=1)
            with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, "maximum file size"):
                model_lifecycle.inspect_archive(archive, max_file_size_bytes=3)
            with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, "uncompressed size exceeds limit"):
                model_lifecycle.inspect_archive(archive, max_total_size_bytes=5)

    def test_archive_rejects_file_parent_conflicts(self) -> None:
        zip_cases = [
            (("weights", "weights/model.gguf"), "below a file member"),
            (("weights/model.gguf", "weights"), "conflicts with existing child member"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for members, expected in zip_cases:
                with self.subTest(kind="zip", members=members):
                    archive = root / f"{members[0].replace('/', '-')}.zip"
                    with zipfile.ZipFile(archive, "w") as handle:
                        for member in members:
                            handle.writestr(member, b"payload")

                    with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, expected):
                        model_lifecycle.inspect_archive(archive)

            tar_cases = [
                (("weights", "weights/model.gguf"), "below a file member"),
                (("weights/model.gguf", "weights"), "conflicts with existing child member"),
            ]
            for members, expected in tar_cases:
                with self.subTest(kind="tar", members=members):
                    archive = root / f"{members[0].replace('/', '-')}.tar"
                    with tarfile.open(archive, "w") as handle:
                        for member in members:
                            data = b"payload"
                            info = tarfile.TarInfo(member)
                            info.size = len(data)
                            handle.addfile(info, io.BytesIO(data))

                    with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, expected):
                        model_lifecycle.inspect_archive(archive)

    def test_archive_rejects_denied_file_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "bad.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("scripts/install.sh", b"#!/bin/sh\n")

            with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, "denied file type"):
                model_lifecycle.inspect_archive(archive)

    def test_install_plan_verifies_content_addressed_blob(self) -> None:
        data = b"tiny model"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            (blob_dir / digest).write_bytes(data)

            manifest = parse_manifest_payload(manifest_payload(digest, len(data)))
            plan = model_lifecycle.build_install_plan(
                manifest,
                root,
                ResourcePolicy(),
                known_aliases={"chat-default"},
            )

            self.assertTrue(plan["can_install"])
            self.assertEqual(plan["files"][0]["status"], "verified")
            model_lifecycle.require_installable(plan, confirmed=True)

    def test_install_plan_blocks_missing_blobs_unknown_aliases_and_unsafe_urls(self) -> None:
        manifest = parse_manifest_payload(manifest_payload("2" * 64, 12, source_url="https://127.0.0.1/model.bin"))
        with tempfile.TemporaryDirectory() as tmp:
            plan = model_lifecycle.build_install_plan(
                manifest,
                Path(tmp),
                ResourcePolicy(),
                known_aliases=set(),
            )

            self.assertFalse(plan["can_install"])
            self.assertIn("source URL is not allowed by import policy", plan["blockers"])
            self.assertIn("one or more content-addressed blobs are missing or failed verification", plan["blockers"])
            self.assertTrue(any("manifest aliases are not defined" in item for item in plan["blockers"]))
            with self.assertRaises(model_lifecycle.ModelLifecycleError):
                model_lifecycle.require_installable(plan, confirmed=True)

    def test_download_plan_supports_direct_url_resume_metadata(self) -> None:
        data = b"partial"
        digest = hashlib.sha256(data + b"-rest").hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            partial = root / "models" / "blobs" / ".partial" / f"{digest}.partial"
            partial.parent.mkdir(parents=True)
            partial.write_bytes(data)
            manifest = parse_manifest_payload(
                manifest_payload(
                    digest,
                    len(data) + 5,
                    source_url="https://downloads.example.org/model.gguf",
                    source_type="direct-url",
                )
            )

            plan = model_lifecycle.build_download_plan(manifest, root)

            self.assertTrue(plan["can_download"])
            self.assertEqual(plan["status"], "downloadable")
            self.assertEqual(plan["existing_partial_bytes"], len(data))
            self.assertEqual(plan["target_sha256"], digest)
            self.assertEqual(plan["file_count"], 1)
            self.assertEqual(len(plan["files"]), 1)

    def test_download_plan_supports_multi_file_base_url(self) -> None:
        first = b"first-model-file"
        second_partial = b"token"
        second_rest = b"izer"
        first_digest = hashlib.sha256(first).hexdigest()
        second_digest = hashlib.sha256(second_partial + second_rest).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            (blob_dir / first_digest).write_bytes(first)
            partial = blob_dir / ".partial" / f"{second_digest}.partial"
            partial.parent.mkdir(parents=True)
            partial.write_bytes(second_partial)
            payload = manifest_payload(
                first_digest,
                len(first),
                source_url="https://downloads.example.org/models/",
                source_type="direct-url",
            )
            payload["files"] = [
                {"path": "weights/model.gguf", "sha256": first_digest, "size_bytes": len(first)},
                {"path": "tokenizer.json", "sha256": second_digest, "size_bytes": len(second_partial) + len(second_rest)},
            ]
            manifest = parse_manifest_payload(payload)

            plan = model_lifecycle.build_download_plan(manifest, root)

            self.assertTrue(plan["can_download"])
            self.assertEqual(plan["status"], "downloadable")
            self.assertFalse(plan["already_available"])
            self.assertEqual(plan["file_count"], 2)
            self.assertEqual(plan["target_size_bytes"], len(first) + len(second_partial) + len(second_rest))
            self.assertEqual(plan["existing_partial_bytes"], len(first) + len(second_partial))
            self.assertEqual(plan["files"][0]["source_url"], "https://downloads.example.org/models/weights/model.gguf")
            self.assertEqual(plan["files"][1]["source_url"], "https://downloads.example.org/models/tokenizer.json")
            self.assertTrue(plan["files"][0]["already_available"])
            self.assertFalse(plan["files"][1]["already_available"])

    def test_download_plan_supports_huggingface_repository_files(self) -> None:
        first = b"readme"
        second_partial = b"token"
        second_rest = b"izer"
        first_digest = hashlib.sha256(first).hexdigest()
        second_digest = hashlib.sha256(second_partial + second_rest).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            (blob_dir / first_digest).write_bytes(first)
            partial = blob_dir / ".partial" / f"{second_digest}.partial"
            partial.parent.mkdir(parents=True)
            partial.write_bytes(second_partial)
            payload = manifest_payload(
                first_digest,
                len(first),
                source_url="https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2",
                source_type="huggingface",
            )
            payload["source"]["revision"] = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
            payload["files"] = [
                {"path": "README.md", "sha256": first_digest, "size_bytes": len(first)},
                {"path": "onnx/model_q4.onnx", "sha256": second_digest, "size_bytes": len(second_partial) + len(second_rest)},
            ]
            manifest = parse_manifest_payload(payload)

            plan = model_lifecycle.build_download_plan(manifest, root)

            self.assertTrue(plan["can_download"])
            self.assertEqual(plan["status"], "downloadable")
            self.assertEqual(plan["file_count"], 2)
            self.assertEqual(plan["files"][0]["source_type"], "huggingface")
            self.assertEqual(
                plan["files"][0]["source_url"],
                "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/README.md",
            )
            self.assertEqual(
                plan["files"][1]["source_url"],
                "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/1110a243fdf4706b3f48f1d95db1a4f5529b4d41/onnx/model_q4.onnx",
            )

    def test_download_url_builders_reject_encoded_file_path_controls(self) -> None:
        direct_payload = manifest_payload(
            "5" * 64,
            12,
            source_url="https://downloads.example.org/models/",
            source_type="direct-url",
        )
        direct_payload["files"] = [
            {"path": "first.gguf", "sha256": "5" * 64, "size_bytes": 12},
            {"path": "second.gguf", "sha256": "6" * 64, "size_bytes": 13},
        ]
        direct_manifest = parse_manifest_payload(direct_payload)
        hf_payload = manifest_payload(
            "7" * 64,
            12,
            source_url="https://huggingface.co/org/model",
            source_type="huggingface",
        )
        hf_manifest = parse_manifest_payload(hf_payload)

        for unsafe_path in (
            "weights/%2e%2e/model.gguf",
            "weights/safe%2Fmodel.gguf",
            "weights/safe%5Cmodel.gguf",
            "weights/safe%3Ftoken.gguf",
            "weights/safe%23fragment.gguf",
            "weights/%00model.gguf",
            "weights/%/model.gguf",
            "weights/%2/model.gguf",
            "weights/%zz/model.gguf",
            "weights/%ffmodel.gguf",
            "C:/model.gguf",
        ):
            with self.subTest(unsafe_path=unsafe_path):
                with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, "unsafe"):
                    model_lifecycle.direct_download_source_url(direct_manifest, SimpleNamespace(path=unsafe_path))
                with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, "unsafe"):
                    model_lifecycle.huggingface_download_source_url(hf_manifest, SimpleNamespace(path=unsafe_path))

    def test_download_plan_blocks_unsafe_huggingface_source(self) -> None:
        payload = manifest_payload(
            "5" * 64,
            12,
            source_url="https://evil.example.org/sentence-transformers/all-MiniLM-L6-v2",
            source_type="huggingface",
        )
        manifest = parse_manifest_payload(payload)
        with tempfile.TemporaryDirectory() as tmp:
            plan = model_lifecycle.build_download_plan(manifest, Path(tmp))

            self.assertFalse(plan["can_download"])
            self.assertIn("source URL is not allowed by import policy", plan["blockers"])
            self.assertTrue(any("huggingface source must use https://huggingface.co" in item for item in plan["blockers"]))

        payload["source"]["url"] = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/blob/main/model.safetensors"
        manifest = parse_manifest_payload(payload)
        with tempfile.TemporaryDirectory() as tmp:
            plan = model_lifecycle.build_download_plan(manifest, Path(tmp))

            self.assertFalse(plan["can_download"])
            self.assertTrue(any("not a file" in item for item in plan["blockers"]))

    def test_download_redirect_policy_allows_only_safe_expected_hosts(self) -> None:
        self.assertEqual(
            model_lifecycle.redirect_url_allowed(
                "https://downloads.example.org/model.gguf",
                "https://downloads.example.org/model.gguf",
                "/cache/model.gguf",
                source_type="direct-url",
            ),
            "https://downloads.example.org/cache/model.gguf",
        )
        with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, "original source host"):
            model_lifecycle.redirect_url_allowed(
                "https://downloads.example.org/model.gguf",
                "https://downloads.example.org/model.gguf",
                "https://cdn.example.org/model.gguf",
                source_type="direct-url",
            )

        redirected = model_lifecycle.redirect_url_allowed(
            "https://huggingface.co/org/model/resolve/main/model.safetensors",
            "https://huggingface.co/org/model/resolve/main/model.safetensors",
            "https://us.aws.cdn.hf.co/xet-bridge-us/model?Signature=temporary",
            source_type="huggingface",
        )
        self.assertTrue(redirected.startswith("https://us.aws.cdn.hf.co/"))
        with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, "unapproved host"):
            model_lifecycle.redirect_url_allowed(
                "https://huggingface.co/org/model/resolve/main/model.safetensors",
                "https://huggingface.co/org/model/resolve/main/model.safetensors",
                "https://example.org/model.safetensors",
                source_type="huggingface",
            )

    def test_download_request_policy_rechecks_current_dns_resolution(self) -> None:
        original_resolver = security.resolve_hostname_addresses
        security.resolve_hostname_addresses = lambda hostname, port: ["127.0.0.1"]
        try:
            with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, "not allowed by import policy"):
                model_lifecycle.download_request_url_allowed(
                    "https://downloads.example.org/model.gguf",
                    "https://downloads.example.org/model.gguf",
                    source_type="direct-url",
                )
            with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, "not allowed by import policy"):
                model_lifecycle.download_request_url_allowed(
                    "https://huggingface.co/org/model/resolve/main/model.safetensors",
                    "https://huggingface.co/org/model/resolve/main/model.safetensors",
                    source_type="huggingface",
                )
        finally:
            security.resolve_hostname_addresses = original_resolver

    def test_download_plan_blocks_multi_file_without_base_url(self) -> None:
        payload = manifest_payload("5" * 64, 12, source_url="https://downloads.example.org/model.gguf", source_type="direct-url")
        payload["files"] = [
            {"path": "first.gguf", "sha256": "5" * 64, "size_bytes": 12},
            {"path": "second.gguf", "sha256": "6" * 64, "size_bytes": 13},
        ]
        manifest = parse_manifest_payload(payload)
        with tempfile.TemporaryDirectory() as tmp:
            plan = model_lifecycle.build_download_plan(manifest, Path(tmp))

            self.assertFalse(plan["can_download"])
            self.assertIn("multi-file direct-url manifests require source.url to end with /", plan["blockers"])

    def test_download_plan_blocks_existing_bad_target_blob(self) -> None:
        data = b"expected"
        digest = hashlib.sha256(data).hexdigest()
        manifest = parse_manifest_payload(
            manifest_payload(
                digest,
                len(data),
                source_url="https://downloads.example.org/model.gguf",
                source_type="direct-url",
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            (blob_dir / digest).write_bytes(b"wrong")

            plan = model_lifecycle.build_download_plan(manifest, root)

            self.assertFalse(plan["can_download"])
            self.assertIn("chat-small.gguf: target blob exists but does not verify", plan["blockers"])

    def test_download_plan_blocks_non_direct_url_and_credentialed_url(self) -> None:
        manifest = parse_manifest_payload(manifest_payload("3" * 64, 12))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(model_lifecycle.ModelLifecycleError):
                model_lifecycle.build_download_plan(manifest, Path(tmp))

        credentialed_urls = [
            "https://downloads.example.org/model.gguf?token=secret",
            "https://downloads.example.org/model.gguf?download_token=secret",
            "https://downloads.example.org/model.gguf?X-Amz-Signature=secret",
            "https://downloads.example.org/model.gguf?X-Goog-Credential=secret",
        ]
        for source_url in credentialed_urls:
            with self.subTest(source_url=source_url):
                credentialed = parse_manifest_payload(
                    manifest_payload(
                        "4" * 64,
                        12,
                        source_url=source_url,
                        source_type="direct-url",
                    )
                )
                with tempfile.TemporaryDirectory() as tmp:
                    plan = model_lifecycle.build_download_plan(credentialed, Path(tmp))
                    self.assertFalse(plan["can_download"])
                    self.assertIn("source URL is not allowed by import policy", plan["blockers"])

        benign_query = parse_manifest_payload(
            manifest_payload(
                "4" * 64,
                12,
                source_url="https://downloads.example.org/model.gguf?download=1",
                source_type="direct-url",
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            plan = model_lifecycle.build_download_plan(benign_query, Path(tmp))
            self.assertTrue(plan["can_download"])
            self.assertNotIn("source URL is not allowed by import policy", plan["blockers"])

    def test_download_plan_requires_license_acceptance_when_declared(self) -> None:
        payload = manifest_payload(
            "5" * 64,
            12,
            source_url="https://downloads.example.org/model.gguf",
            source_type="direct-url",
        )
        payload["license"]["acceptance_required"] = True
        manifest = parse_manifest_payload(payload)
        with tempfile.TemporaryDirectory() as tmp:
            blocked = model_lifecycle.build_download_plan(manifest, Path(tmp))
            accepted = model_lifecycle.build_download_plan(manifest, Path(tmp), accept_license=True)

            self.assertFalse(blocked["can_download"])
            self.assertTrue(blocked["requires_license_acceptance"])
            self.assertFalse(blocked["license_accepted"])
            self.assertIn("licence acceptance is required", blocked["blockers"])
            self.assertTrue(accepted["can_download"])
            self.assertTrue(accepted["license_accepted"])
            self.assertNotIn("licence acceptance is required", accepted["blockers"])

    def test_internal_placeholder_files_do_not_require_blob(self) -> None:
        manifest = parse_manifest_payload(
            {
                **manifest_payload("0" * 64, 1),
                "id": "placeholder",
                "display_name": "Placeholder",
                "source": {"type": "catalog", "url": "https://models.ai.b1.germering/internal/placeholders", "revision": "0.1.0"},
                "files": [{"path": "internal-placeholder", "sha256": "0" * 64, "size_bytes": 1, "format": "internal"}],
                "license": {"name": "internal", "redistribution": "inference-only"},
                "execution_modes": ["hosted-inference"],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            plan = model_lifecycle.build_install_plan(manifest, Path(tmp), ResourcePolicy(), known_aliases={"chat-default"})

            self.assertTrue(plan["can_install"])
            self.assertEqual(plan["files"][0]["status"], "internal-placeholder")

    def test_runtime_views_hardlink_verified_blobs_and_quarantine_views(self) -> None:
        data = b"runtime view model"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            blob_path = blob_dir / digest
            blob_path.write_bytes(data)
            manifest = parse_manifest_payload(
                {
                    **manifest_payload(digest, len(data)),
                    "runtimes": ["localai", "comfyui"],
                    "preferred_runtime": "localai",
                }
            )

            views = model_lifecycle.create_runtime_views(manifest, root)

            self.assertEqual({view["runtime"] for view in views}, {"localai", "comfyui"})
            local_file = root / "models" / "runtime-views" / "localai" / "chat-small" / "1.0.0" / "chat-small.gguf"
            comfy_file = root / "models" / "runtime-views" / "comfyui" / "chat-small" / "1.0.0" / "chat-small.gguf"
            self.assertTrue(local_file.is_file())
            self.assertTrue(comfy_file.is_file())
            self.assertTrue(os.path.samefile(blob_path, local_file))
            self.assertTrue(os.path.samefile(blob_path, comfy_file))
            self.assertFalse(local_file.is_symlink())
            self.assertTrue((local_file.parent / "manifest.b1.json").is_file())

            moved = model_lifecycle.quarantine_runtime_views(manifest, root, timestamp=datetime(2026, 7, 22, 12, 0, tzinfo=UTC))

            self.assertEqual({item["status"] for item in moved}, {"quarantined"})
            self.assertFalse(local_file.exists())
            self.assertTrue(
                (
                    root
                    / "models"
                    / "quarantine"
                    / "runtime-views"
                    / "localai"
                    / "chat-small"
                    / "1.0.0-20260722T120000Z"
                    / "chat-small.gguf"
                ).is_file()
            )
            self.assertTrue(blob_path.is_file())

    def test_blob_quarantine_requires_quarantined_model_and_refuses_shared_blobs(self) -> None:
        data = b"shared blob"
        digest = hashlib.sha256(data).hexdigest()
        manifest = parse_manifest_payload(manifest_payload(digest, len(data)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            (blob_dir / digest).write_bytes(data)
            records = [
                {"id": "chat-small", "version": "1.0.0", "status": "quarantined", "manifest": manifest.to_dict()},
                {
                    "id": "chat-other",
                    "version": "1.0.0",
                    "status": "installed",
                    "manifest": {**manifest.to_dict(), "id": "chat-other", "aliases": []},
                },
            ]

            installed_plan = model_lifecycle.build_blob_quarantine_plan(
                manifest,
                root,
                records,
                model_status="installed",
                timestamp=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
            )
            self.assertFalse(installed_plan["can_quarantine"])
            self.assertIn("model record must be quarantined before authoritative blobs can be quarantined", installed_plan["blockers"])

            shared_plan = model_lifecycle.build_blob_quarantine_plan(
                manifest,
                root,
                records,
                model_status="quarantined",
                timestamp=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
            )
            self.assertFalse(shared_plan["can_quarantine"])
            self.assertEqual(shared_plan["blobs"][0]["referenced_by"], ["chat-other@1.0.0"])
            self.assertTrue(any("referenced by other model records" in item for item in shared_plan["blockers"]))

    def test_blob_quarantine_moves_unused_authoritative_blob(self) -> None:
        data = b"unused blob"
        digest = hashlib.sha256(data).hexdigest()
        manifest = parse_manifest_payload(manifest_payload(digest, len(data)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            source = blob_dir / digest
            source.write_bytes(data)
            records = [{"id": "chat-small", "version": "1.0.0", "status": "quarantined", "manifest": manifest.to_dict()}]

            result = model_lifecycle.quarantine_authoritative_blobs(
                manifest,
                root,
                records,
                model_status="quarantined",
                confirmed=True,
                timestamp=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
            )

            destination = root / "models" / "quarantine" / "blobs" / "chat-small" / "1.0.0-20260722T120000Z" / digest
            self.assertEqual(result["status"], "quarantined")
            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), data)
            self.assertEqual(result["moved"][0]["quarantine_path"], str(destination))

    def test_blob_quarantine_rechecks_blob_before_move(self) -> None:
        data = b"tiny model"
        changed = b"tiny MODEL"
        digest = hashlib.sha256(data).hexdigest()
        manifest = parse_manifest_payload(manifest_payload(digest, len(data)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            source = blob_dir / digest
            source.write_bytes(data)
            records = [{"id": "chat-small", "version": "1.0.0", "status": "quarantined", "manifest": manifest.to_dict()}]
            original_sha256_file = model_lifecycle.sha256_file
            calls = 0

            def racing_sha256_file(path: Path) -> str:
                nonlocal calls
                result = original_sha256_file(path)
                calls += 1
                if calls == 1:
                    path.write_bytes(changed)
                return result

            model_lifecycle.sha256_file = racing_sha256_file
            try:
                with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, "changed before quarantine move"):
                    model_lifecycle.quarantine_authoritative_blobs(
                        manifest,
                        root,
                        records,
                        model_status="quarantined",
                        confirmed=True,
                    )
            finally:
                model_lifecycle.sha256_file = original_sha256_file
            self.assertEqual(source.read_bytes(), changed)

    def test_blob_quarantine_retention_plans_and_deletes_old_sets(self) -> None:
        old_data = b"old quarantined blob"
        new_data = b"new quarantined blob"
        old_sha = hashlib.sha256(old_data).hexdigest()
        new_sha = hashlib.sha256(new_data).hexdigest()
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            quarantine_root = root / "models" / "quarantine" / "blobs" / "chat-small"
            old_set = quarantine_root / "1.0.0-20260701T120000Z"
            new_set = quarantine_root / "1.0.0-20260722T120000Z"
            invalid_set = quarantine_root / "not-a-valid-set"
            old_set.mkdir(parents=True)
            new_set.mkdir(parents=True)
            invalid_set.mkdir(parents=True)
            (old_set / old_sha).write_bytes(old_data)
            (new_set / new_sha).write_bytes(new_data)
            (invalid_set / "not-a-sha").write_bytes(b"preserve")

            plan = model_lifecycle.build_blob_quarantine_retention_plan(
                root,
                delete_older_than_days=7,
                now=now,
            )

            self.assertEqual(plan["status"], "planned")
            self.assertEqual(plan["candidate_count"], 1)
            self.assertEqual(plan["candidates"][0]["quarantine_set"], "1.0.0-20260701T120000Z")
            self.assertEqual(plan["kept_count"], 1)
            self.assertEqual(plan["invalid_preserved_count"], 1)
            self.assertEqual(plan["total_reclaimable_bytes"], len(old_data))

            with self.assertRaisesRegex(model_lifecycle.ModelLifecycleError, "requires explicit confirmation"):
                model_lifecycle.apply_blob_quarantine_retention_plan(
                    root,
                    delete_older_than_days=7,
                    confirmed=False,
                    now=now,
                )

            cleaned = model_lifecycle.apply_blob_quarantine_retention_plan(
                root,
                delete_older_than_days=7,
                confirmed=True,
                now=now,
            )

            self.assertEqual(cleaned["status"], "cleaned")
            self.assertEqual(cleaned["deleted_count"], 1)
            self.assertFalse(old_set.exists())
            self.assertTrue((new_set / new_sha).is_file())
            self.assertTrue((invalid_set / "not-a-sha").is_file())

    def test_blob_quarantine_retention_preserves_symlinks_and_unsafe_children(self) -> None:
        data = b"quarantined blob"
        digest = hashlib.sha256(data).hexdigest()
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            quarantine_root = root / "models" / "quarantine" / "blobs" / "chat-small"
            unsafe_set = quarantine_root / "1.0.0-20260701T120000Z"
            unsafe_set.mkdir(parents=True)
            (unsafe_set / digest).write_bytes(data)
            (unsafe_set / "nested").mkdir()

            plan = model_lifecycle.build_blob_quarantine_retention_plan(
                root,
                delete_older_than_days=7,
                now=now,
            )

            self.assertEqual(plan["candidate_count"], 0)
            self.assertEqual(plan["invalid_preserved_count"], 1)
            self.assertIn("not a regular file", plan["invalid_preserved"][0]["reason"])

    def test_runtime_view_creation_refuses_existing_symlink(self) -> None:
        data = b"runtime view model"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blob_dir = root / "models" / "blobs"
            blob_dir.mkdir(parents=True)
            (blob_dir / digest).write_bytes(data)
            link_path = root / "models" / "runtime-views" / "localai" / "chat-small" / "1.0.0" / "chat-small.gguf"
            link_path.parent.mkdir(parents=True)
            link_path.symlink_to(root / "outside")
            manifest = parse_manifest_payload(manifest_payload(digest, len(data)))

            with self.assertRaises(model_lifecycle.ModelLifecycleError):
                model_lifecycle.create_runtime_views(manifest, root)


if __name__ == "__main__":
    unittest.main()
