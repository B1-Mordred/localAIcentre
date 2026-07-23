from __future__ import annotations

import importlib.util
import io
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALLER_PATH = ROOT / "services" / "audio-cpu" / "scripts" / "install_piper.py"


def load_installer():
    spec = importlib.util.spec_from_file_location("b1_install_piper", INSTALLER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Piper installer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_tar(path: Path, members: list[tuple[str, bytes, str | None]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, payload, linkname in members:
            info = tarfile.TarInfo(name)
            if linkname is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = linkname
                archive.addfile(info)
                continue
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


class PiperInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.installer = load_installer()

    def test_pinned_release_metadata_matches_dockerfile(self) -> None:
        dockerfile = (ROOT / "services" / "audio-cpu" / "Dockerfile").read_text(encoding="utf-8")
        self.assertEqual(self.installer.DEFAULT_RELEASE, "2023.11.14-2")
        self.assertEqual(self.installer.DEFAULT_ASSET, "piper_linux_x86_64.tar.gz")
        self.assertEqual(
            self.installer.DEFAULT_SHA256,
            "a50cb45f355b7af1f6d758c1b360717877ba0a398cc8cbe6d2a7a3a26e225992",
        )
        self.assertEqual(
            self.installer.RELEASE_URL,
            "https://github.com/rhasspy/piper/releases/download/{release}/{asset}",
        )
        self.assertIn("ARG B1_INSTALL_PIPER=true", dockerfile)
        self.assertIn(f"ARG B1_PIPER_RELEASE={self.installer.DEFAULT_RELEASE}", dockerfile)
        self.assertIn(f"ARG B1_PIPER_SHA256={self.installer.DEFAULT_SHA256}", dockerfile)
        self.assertIn("python /tmp/install_piper.py", dockerfile)

    def test_archive_validation_accepts_relative_piper_members_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "piper.tar.gz"
            make_tar(
                archive,
                [
                    ("piper/piper", b"binary", None),
                    ("piper/libpiper_phonemize.so", b"", "libpiper_phonemize.so.1"),
                ],
            )
            self.installer.validate_archive(archive)

    def test_archive_validation_rejects_traversal_absolute_and_unsafe_links(self) -> None:
        cases = [
            ("../piper", b"", None),
            ("/piper/piper", b"", None),
            ("other/piper", b"", None),
            ("piper/link", b"", "/etc/passwd"),
            ("piper/link", b"", "../escape"),
        ]
        for name, payload, linkname in cases:
            with self.subTest(name=name, linkname=linkname), tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "piper.tar.gz"
                make_tar(archive, [(name, payload, linkname)])
                with self.assertRaises(self.installer.UnsafeArchiveError):
                    self.installer.validate_archive(archive)


if __name__ == "__main__":
    unittest.main()
