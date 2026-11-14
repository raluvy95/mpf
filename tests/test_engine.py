"""The playback engine must stay light enough to run as the resident background player."""

import subprocess
import sys
import unittest

HEAVY = ("numpy", "blessed")


class TestEngineWeight(unittest.TestCase):
    def test_engine_imports_neither_numpy_nor_blessed(self):
        # A fresh interpreter: this test process has long since imported both through other tests.
        code = "import sys, mpf_core.engine, mpf_core.server, mpf_core.client, mpf_core.protocol, mpf_core.control; print(','.join(m for m in %r if m in sys.modules))" % (HEAVY,)
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "", f"the engine pulled in: {result.stdout.strip()}")


if __name__ == "__main__":
    unittest.main()
