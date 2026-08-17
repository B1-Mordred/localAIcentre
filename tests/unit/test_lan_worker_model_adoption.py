from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from fastapi import HTTPException  # noqa: E402
    from app import main, model_lifecycle  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy", "asyncpg", "cryptography", "websockets"}:
        raise
    HTTPException = None  # type: ignore[assignment]
    main = None  # type: ignore[assignment]
    model_lifecycle = None  # type: ignore[assignment]
    AuthContext = None  # type: ignore[assignment]
    Role = None  # type: ignore[assignment]
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


MODEL_ID = "b1-poolside-laguna-s-2.1-q4km-compact-p40"
REVISION = "edd093522473dc7313b0738d8b4116b7f8b9745f"
MODEL_SHA256 = "a34c74e46688122bef83122f4133031bababbefcf57436dde97048c91e2cc6ff"
IMAGE_DIGEST = "sha256:" + "b" * 64
LLAMA_COMMIT = "06f8cebd7fe728687be3d19f8bdedb70d75883af"
PROFILES_SHA256 = "c" * 64
DEEPSEEK_MODEL_ID = "b1-unsloth-deepseek-v4-flash-0731-main"
DEEPSEEK_REVISION = "fbbb5b93fb787c21338159b0af3318bb3f4d9768"


def manifest_payload() -> dict[str, Any]:
    return {
        "id": MODEL_ID,
        "version": REVISION,
        "display_name": "Laguna S 2.1 Q4_K_M Compact P40",
        "description": "Attested managed P40 runtime model.",
        "modality": "llm",
        "operations": ["chat"],
        "source": {
            "type": "huggingface",
            "url": "https://huggingface.co/poolside/Laguna-S-2.1-GGUF",
            "revision": REVISION,
        },
        "files": [
            {
                "path": "laguna-s-2.1-Q4_K_M.gguf",
                "sha256": MODEL_SHA256,
                "size_bytes": 68248760064,
                "format": "gguf",
                "quantization": "Q4_K_M",
            }
        ],
        "runtimes": ["lan-localai-worker"],
        "preferred_runtime": "lan-localai-worker",
        "resource_estimate": {"vram_gib": 21.0, "ram_gib": 78.0, "disk_gib": 63.562, "context_tokens": 16384},
        "license": {
            "name": "OpenMDW-1.1",
            "url": "https://huggingface.co/poolside/Laguna-S-2.1/blob/00af5a51782109b587a3b3bbf11875e566036fa7/LICENSE.md",
            "attribution": "Poolside Laguna S 2.1 official GGUF conversion",
            "redistribution": "inference-only",
            "acceptance_required": True,
        },
        "execution_modes": ["hosted-inference"],
        "aliases": ["laguna-s-fast"],
        "visibility_roles": ["admin", "operator", "creator", "user", "service"],
        "permissions": {
            "visible_to": ["admin", "operator", "creator", "user", "service"],
            "installable_by": ["admin", "operator"],
            "inference_roles": ["admin", "operator", "creator", "user", "service"],
        },
        "runtime_adapter_versions": {"lan-localai-worker": "b1-runtime-adapter/v1alpha1"},
        "runtime_smoke": {
            "schema": "b1-ai-hub-runtime-smoke/v1",
            "description": "Bounded managed P40 chat smoke.",
            "lan-localai-worker": {
                "request": {"method": "POST", "path": "/v1/chat/completions"},
                "timeout_seconds": 1800,
            },
        },
        "installation_status": "available",
    }


def attestation_payload() -> dict[str, Any]:
    return {
        "status": "ok",
        "runtime": "lan-localai-worker",
        "backend": "laguna",
        "action": "attest",
        "model_id": MODEL_ID,
        "version": REVISION,
        "files": [
            {
                "role": "main",
                "path": "laguna-s-2.1-Q4_K_M.gguf",
                "sha256": MODEL_SHA256,
                "size_bytes": 68248760064,
                "read_only": True,
            }
        ],
        "llama_commit": LLAMA_COMMIT,
        "image_digest": IMAGE_DIGEST,
        "profiles_sha256": PROFILES_SHA256,
    }


def deepseek_manifest_payload() -> dict[str, Any]:
    payload = manifest_payload()
    payload.update(
        {
            "id": DEEPSEEK_MODEL_ID,
            "version": DEEPSEEK_REVISION,
            "display_name": "DeepSeek V4 Flash 0731 UD-IQ3_XXS P40",
            "source": {
                "type": "huggingface",
                "url": "https://huggingface.co/unsloth/DeepSeek-V4-Flash-0731-GGUF",
                "revision": DEEPSEEK_REVISION,
            },
            "files": [
                {
                    "path": f"main-{index}.gguf",
                    "sha256": f"{index}" * 64,
                    "size_bytes": 1024 * index,
                    "format": "gguf",
                    "quantization": "UD-IQ3_XXS",
                }
                for index in range(1, 5)
            ],
            "runtimes": ["lan-deepseek-worker"],
            "preferred_runtime": "lan-deepseek-worker",
            "license": {
                "name": "MIT",
                "url": "https://huggingface.co/unsloth/DeepSeek-V4-Flash-0731-GGUF",
                "attribution": "Unsloth DeepSeek V4 Flash GGUF",
                "redistribution": "downloadable",
                "acceptance_required": False,
            },
            "aliases": ["deepseek-main"],
            "runtime_adapter_versions": {"lan-deepseek-worker": "b1-runtime-adapter/v1alpha1"},
            "runtime_smoke": {
                "schema": "b1-ai-hub-runtime-smoke/v1",
                "description": "Bounded managed DeepSeek chat smoke.",
                "lan-deepseek-worker": {
                    "request": {"method": "POST", "path": "/v1/chat/completions"},
                    "timeout_seconds": 2400,
                },
            },
        }
    )
    return payload


def deepseek_attestation_payload() -> dict[str, Any]:
    manifest = deepseek_manifest_payload()
    return {
        "status": "ok",
        "runtime": "lan-deepseek-worker",
        "backend": "deepseek-v4",
        "action": "attest",
        "model_id": DEEPSEEK_MODEL_ID,
        "version": DEEPSEEK_REVISION,
        "files": [
            {
                "role": f"main-shard-{index}",
                "path": item["path"],
                "sha256": item["sha256"],
                "size_bytes": item["size_bytes"],
                "read_only": True,
            }
            for index, item in enumerate(manifest["files"], start=1)
        ],
        "llama_commit": "030ebb558a5820b444a8f836ed5cdd46c9b4bd7a",
        "image_digest": IMAGE_DIGEST,
        "profiles_sha256": PROFILES_SHA256,
    }


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class LanWorkerModelAdoptionTests(unittest.TestCase):
    def patch_attr(self, name: str, value: Any) -> None:
        original = getattr(main, name)
        setattr(main, name, value)
        self.addCleanup(lambda: setattr(main, name, original))

    def test_manifest_must_be_lan_worker_only(self) -> None:
        manifest = model_lifecycle.parse_uploaded_manifest(manifest_payload())
        main.require_lan_worker_only_manifest(manifest)
        payload = manifest_payload()
        payload["runtimes"] = ["lan-localai-worker", "localai"]
        payload["preferred_runtime"] = "lan-localai-worker"
        with self.assertRaisesRegex(HTTPException, "exactly one managed LAN worker"):
            main.require_lan_worker_only_manifest(model_lifecycle.parse_uploaded_manifest(payload))

    def test_deepseek_split_manifest_uses_distinct_managed_runtime(self) -> None:
        manifest = model_lifecycle.parse_uploaded_manifest(deepseek_manifest_payload())
        main.require_lan_worker_only_manifest(manifest)
        self.assertEqual(manifest.preferred_runtime, "lan-deepseek-worker")
        payload = deepseek_manifest_payload()
        payload["files"] = payload["files"][:3]
        with self.assertRaisesRegex(HTTPException, "artifact count"):
            main.require_lan_worker_only_manifest(model_lifecycle.parse_uploaded_manifest(payload))

    def test_deepseek_attestation_requires_runtime_and_backend_identity(self) -> None:
        manifest = model_lifecycle.parse_uploaded_manifest(deepseek_manifest_payload())

        class FakeResponse:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return deepseek_attestation_payload()

        class FakeClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "FakeClient":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            async def post(self, *args: Any, **kwargs: Any) -> FakeResponse:
                return FakeResponse()

        adapter = SimpleNamespace(
            configured=True,
            configuration_error=None,
            httpx_client_kwargs=lambda: {"trust_env": False},
            url_for=lambda path: "https://p40-worker.b1.germering:9443/deepseek" + path,
            request_headers=lambda: {"Authorization": "Bearer redacted"},
        )
        registry = SimpleNamespace(adapter=lambda name: adapter if name == "lan-deepseek-worker" else None)
        self.patch_attr("runtime_registry_snapshot", lambda: registry)
        original_client = main.httpx.AsyncClient
        main.httpx.AsyncClient = FakeClient
        self.addCleanup(lambda: setattr(main.httpx, "AsyncClient", original_client))

        result = asyncio.run(main.attest_lan_worker_manifest(manifest))
        self.assertEqual(result["runtime"], "lan-deepseek-worker")
        self.assertEqual(result["backend"], "deepseek-v4")
        self.assertEqual(len(result["files"]), 4)
        self.assertTrue(all(item["read_only"] is True for item in result["files"]))

    def test_attestation_requires_exact_immutable_file_identity(self) -> None:
        manifest = model_lifecycle.parse_uploaded_manifest(manifest_payload())

        class FakeResponse:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return attestation_payload()

        class FakeClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "FakeClient":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            async def post(self, *args: Any, **kwargs: Any) -> FakeResponse:
                return FakeResponse()

        adapter = SimpleNamespace(
            configured=True,
            configuration_error=None,
            httpx_client_kwargs=lambda: {"trust_env": False},
            url_for=lambda path: "https://p40-worker.b1.germering:9443" + path,
            request_headers=lambda: {"Authorization": "Bearer redacted"},
        )
        registry = SimpleNamespace(adapter=lambda name: adapter if name == "lan-localai-worker" else None)
        self.patch_attr("runtime_registry_snapshot", lambda: registry)
        original_client = main.httpx.AsyncClient
        main.httpx.AsyncClient = FakeClient
        self.addCleanup(lambda: setattr(main.httpx, "AsyncClient", original_client))

        result = asyncio.run(main.attest_lan_worker_manifest(manifest))
        self.assertEqual(result["files"][0]["sha256"], MODEL_SHA256)
        self.assertEqual(result["image_digest"], IMAGE_DIGEST)

        bad = attestation_payload()
        bad["files"][0]["size_bytes"] += 1

        class BadResponse(FakeResponse):
            def json(self) -> dict[str, Any]:
                return bad

        async def bad_post(_self: Any, *args: Any, **kwargs: Any) -> BadResponse:
            return BadResponse()

        FakeClient.post = bad_post
        with self.assertRaisesRegex(HTTPException, "hashes or sizes"):
            asyncio.run(main.attest_lan_worker_manifest(manifest))

    def test_attested_manifest_is_remote_and_auditable(self) -> None:
        manifest = model_lifecycle.parse_uploaded_manifest(manifest_payload())
        installed = main.manifest_with_lan_worker_attestation(manifest, attestation_payload())
        runs = installed.measurements["runs"]
        self.assertEqual(runs[-1]["type"], "worker-artifact-attestation")
        self.assertEqual(runs[-1]["hook"]["image_digest"], IMAGE_DIGEST)
        public = main.public_model_record(
            {
                "id": installed.id,
                "version": installed.version,
                "manifest": installed.to_dict(),
            }
        )
        self.assertEqual(public["runtime_views"], [])
        self.assertTrue(public["remote_runtime"]["artifact_attested"])

    def test_confirmed_operator_adoption_persists_without_local_runtime_view(self) -> None:
        records: list[dict[str, Any]] = []
        audits: list[dict[str, Any]] = []

        async def authenticate(_: str | None = None) -> Any:
            return AuthContext(subject_id="operator_1", role=Role.OPERATOR, scopes=frozenset({"models:write"}))

        class FakeDatabase:
            async def get_model_record(self, model_id: str, version: str) -> None:
                return None

            async def upsert_model_record(self, payload: dict[str, Any]) -> dict[str, Any]:
                records.append(dict(payload))
                return dict(payload)

        async def no_conflicts(_: Any) -> list[dict[str, str]]:
            return []

        async def attest(_: Any) -> dict[str, Any]:
            return attestation_payload()

        async def refresh_catalog() -> None:
            return None

        async def refresh_workflows() -> dict[str, Any]:
            return {"count": 0, "refreshed": []}

        async def audit(auth: Any, event_type: str, **metadata: Any) -> None:
            audits.append({"subject": auth.subject_id, "event_type": event_type, **metadata})

        self.patch_attr("authenticate", authenticate)
        self.patch_attr("database", FakeDatabase())
        self.patch_attr("installed_model_alias_conflicts", no_conflicts)
        self.patch_attr("attest_lan_worker_manifest", attest)
        self.patch_attr("refresh_catalog_cache", refresh_catalog)
        self.patch_attr("refresh_workflow_dependency_statuses", refresh_workflows)
        self.patch_attr("record_audit_event", audit)

        result = asyncio.run(
            main.admin_model_adopt_lan_worker(
                main.LanWorkerModelAdoptionRequest(manifest=manifest_payload(), confirm=True, accept_license=True),
                authorization="Bearer test",
            )
        )
        self.assertEqual(result["runtime_views"], [])
        self.assertEqual(records[0]["preferred_runtime"], "lan-localai-worker")
        self.assertEqual(records[0]["status"], "installed")
        self.assertEqual(audits[0]["event_type"], "model.lan_worker_adopted")


if __name__ == "__main__":
    unittest.main()
