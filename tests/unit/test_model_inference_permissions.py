from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

try:
    from app import main  # noqa: E402
    from app.auth import AuthContext, Role  # noqa: E402
    from app.catalog import (  # noqa: E402
        AliasDefinition,
        ManifestFile,
        ManifestSource,
        ModelCatalog,
        ModelLicense,
        ModelManifest,
        ModelPermissions,
        ModelResourceEstimate,
    )
    from app.scheduler import ResourcePolicy  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local test environment packages
    if exc.name not in {"fastapi", "httpx", "pydantic", "redis", "sqlalchemy", "websockets"}:
        raise
    main = None
    MISSING_DEPENDENCY = exc.name
else:
    MISSING_DEPENDENCY = ""


def catalog_with_inference_roles(roles: list[str] | None) -> ModelCatalog:
    return ModelCatalog(
        aliases=[
            AliasDefinition(
                alias="chat-default",
                modality="llm",
                preferred_runtime="localai",
                status="installed",
            )
        ],
        manifests=[
            ModelManifest(
                id="chat-small",
                version="1.0.0",
                display_name="Chat Small",
                modality="llm",
                operations=["chat"],
                source=ManifestSource(type="catalog", url="https://models.ai.b1.germering/test", revision="1.0.0"),
                files=[ManifestFile(path="chat-small.gguf", sha256="0" * 64, size_bytes=1)],
                runtimes=["localai"],
                preferred_runtime="localai",
                resource_estimate=ModelResourceEstimate(vram_gib=1.0, ram_gib=1.0, disk_gib=1.0),
                license=ModelLicense(name="test", redistribution="inference-only"),
                execution_modes=["hosted-inference"],
                aliases=["chat-default"],
                permissions=ModelPermissions(inference_roles=roles or []),
            )
        ],
        policy=ResourcePolicy(),
    )


@unittest.skipIf(main is None, f"{MISSING_DEPENDENCY} is not installed in this lightweight test environment")
class ModelInferencePermissionTests(unittest.TestCase):
    def patch_catalog(self, catalog: ModelCatalog) -> None:
        original = main.catalog_snapshot
        main.catalog_snapshot = lambda: catalog
        self.addCleanup(lambda: setattr(main, "catalog_snapshot", original))

    def test_manifest_inference_roles_block_unauthorized_service_clients(self) -> None:
        self.patch_catalog(catalog_with_inference_roles(["admin"]))
        auth = AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"inference:write"}))

        with self.assertRaises(main.HTTPException) as caught:
            main.require_catalog_alias("chat-default", "llm", auth=auth)

        self.assertEqual(caught.exception.status_code, 403)
        self.assertIn("cannot be used for inference", caught.exception.detail)

    def test_admin_wildcard_bypasses_manifest_inference_roles(self) -> None:
        self.patch_catalog(catalog_with_inference_roles(["operator"]))
        auth = AuthContext(subject_id="admin_1", role=Role.ADMIN, scopes=frozenset({"*"}))

        alias = main.require_catalog_alias("chat-default", "llm", auth=auth)

        self.assertEqual(alias.alias.alias, "chat-default")

    def test_missing_manifest_inference_roles_keep_existing_open_policy(self) -> None:
        self.patch_catalog(catalog_with_inference_roles(None))
        auth = AuthContext(subject_id="client_1", role=Role.SERVICE, scopes=frozenset({"inference:write"}))

        alias = main.require_catalog_alias("chat-default", "llm", auth=auth)

        self.assertEqual(alias.alias.alias, "chat-default")


if __name__ == "__main__":
    unittest.main()
