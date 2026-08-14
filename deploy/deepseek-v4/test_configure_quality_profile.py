from __future__ import annotations

import unittest

import configure_quality_profile as quality


class ConfigureQualityProfileTests(unittest.TestCase):
    def document(self) -> dict[str, object]:
        return {
            "schema_version": "b1.deepseek.runtime_profiles.v1",
            "profiles": [
                {"model_id": "b1-unsloth-deepseek-v4-flash-0731-main", "aliases": ["deepseek-main"], "server_args": ["--ctx-size", "32768", "--reasoning-budget", "384"]},
                {"model_id": "b1-unsloth-deepseek-v4-flash-0731-quality", "aliases": ["deepseek-quality"], "server_args": ["--ctx-size", "32768", "--reasoning-budget", "512", "--verbose"]},
            ],
        }

    def test_only_quality_policy_changes(self) -> None:
        before = self.document()
        after = quality.configure(before)
        self.assertEqual(after["profiles"][0], before["profiles"][0])
        args = after["profiles"][1]["server_args"]
        self.assertEqual(args[:3], ["--ctx-size", "32768", "--verbose"])
        for option, value in quality.VALUE_OPTIONS.items():
            self.assertEqual(args.count(option), 1)
            self.assertEqual(args[args.index(option) + 1], value)

    def test_existing_quality_profile_is_required(self) -> None:
        document = self.document()
        document["profiles"] = document["profiles"][:1]
        with self.assertRaisesRegex(ValueError, "exactly one"):
            quality.configure(document)


if __name__ == "__main__":
    unittest.main()
