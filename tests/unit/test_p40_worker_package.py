from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SCRIPT = ROOT / "deploy" / "scripts" / "package-p40-worker.sh"


class P40WorkerPackageTests(unittest.TestCase):
    def test_checksum_uses_portable_archive_basename(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp) / "nested" / "worker-update"
            archive = output_dir / "b1-p40-worker-test.tar.gz"
            env = os.environ.copy()
            env["B1_P40_INCLUDE_RUNTIME_TOKEN"] = "false"

            subprocess.run(
                [str(PACKAGE_SCRIPT), str(archive)],
                cwd=ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

            checksum = archive.with_name(f"{archive.name}.sha256")
            digest, recorded_path = checksum.read_text(encoding="utf-8").strip().split(maxsplit=1)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertEqual(recorded_path, archive.name)
            subprocess.run(
                ["sha256sum", "-c", checksum.name],
                cwd=output_dir,
                check=True,
                capture_output=True,
                text=True,
            )


if __name__ == "__main__":
    unittest.main()
