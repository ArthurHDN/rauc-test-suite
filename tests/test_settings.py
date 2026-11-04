"""Unit tests of the settings lookup (TARGET_* with the legacy JETSON_* names)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fakes  # noqa: F401 - puts libraries/ on sys.path

import audit_common as ac


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_file = Path(self.tmp.name) / ".env"
        # Start from an empty environment so a developer's own settings do not leak into the tests.
        patcher = mock.patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ["RAUC_TESTS_ENV_FILE"] = str(self.env_file)

    def write(self, text):
        self.env_file.write_text(text, encoding="utf-8")

    def test_new_name(self):
        self.write("TARGET_HOST=10.0.0.1\n")
        self.assertEqual("10.0.0.1", ac.get_setting("TARGET_HOST"))

    def test_legacy_name_in_the_env_file(self):
        self.write("JETSON_HOST=10.0.0.2\nDEVICE=raspberrypi4-64\n")
        self.assertEqual("10.0.0.2", ac.get_setting("TARGET_HOST"))
        self.assertEqual("raspberrypi4-64", ac.get_setting("DEVICE"))

    def test_new_name_wins_over_legacy(self):
        self.write("JETSON_HOST=old\nTARGET_HOST=new\n")
        self.assertEqual("new", ac.get_setting("TARGET_HOST"))

    def test_process_environment_wins_over_the_file(self):
        self.write("TARGET_HOST=file\n")
        os.environ["JETSON_HOST"] = "environment"
        self.assertEqual("environment", ac.get_setting("TARGET_HOST"))

    def test_empty_value_is_not_missing(self):
        self.write("TARGET_PASSWORD=\n")
        self.assertEqual("", ac.get_setting("TARGET_PASSWORD", "default"))

    def test_missing_value_gives_the_default(self):
        self.write("")
        self.assertEqual("22", ac.get_setting("TARGET_PORT", "22"))
        self.assertIsNone(ac.get_setting("DEVICE"))

    def test_legacy_env_file_variable(self):
        del os.environ["RAUC_TESTS_ENV_FILE"]
        os.environ["JETSON_ENV_FILE"] = str(self.env_file)
        self.assertEqual(self.env_file, ac.env_file_path())

    def test_quotes_and_export_are_handled(self):
        self.write("export TARGET_USER='root'\nTARGET_PASSWORD=\"a b\"\n")
        self.assertEqual("root", ac.get_setting("TARGET_USER"))
        self.assertEqual("a b", ac.get_setting("TARGET_PASSWORD"))


if __name__ == "__main__":
    unittest.main()
