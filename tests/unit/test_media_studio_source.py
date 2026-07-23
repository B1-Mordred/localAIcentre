from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MEDIA_STUDIO = ROOT / "web" / "media-studio" / "src" / "main.tsx"


class MediaStudioSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = MEDIA_STUDIO.read_text(encoding="utf-8")

    def test_recovery_required_jobs_are_terminal_for_progress_and_cancel_controls(self) -> None:
        self.assertRegex(self.source, r'TERMINAL_STATES = new Set\(\[[^\]]*"recovery_required"')
        self.assertIn('disabled={!job || TERMINAL_STATES.has(job.state)}', self.source)
        self.assertIn('if (!currentJob || TERMINAL_STATES.has(currentJob.state)) return;', self.source)


if __name__ == "__main__":
    unittest.main()
