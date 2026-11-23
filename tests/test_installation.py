import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(importlib.util.find_spec("astrbot") is not None, "Requires AstrBot SDK")
class InstallationTests(unittest.TestCase):
    def test_actual_astrbot_loader_configuration_worker_and_hot_reload(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(root / "tests/installation_probe.py"), directory, str(root)],
                env={**os.environ, "ASTRBOT_ROOT": directory, "PYTHONIOENCODING": "utf-8"},
                capture_output=True, text=True, encoding="utf-8", timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self.assertEqual(result.returncode, 0, result.stdout[-2500:] + result.stderr[-2500:])
            report = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertTrue(report["loaded"])
            self.assertTrue(report["native_configuration"])
            self.assertTrue(report["hot_reload"])
            self.assertEqual(report["default_targets"], [])
