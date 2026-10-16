"""A corrupt spend ledger must fail closed and must not be rewritten as $0."""

import sys
import types
import tempfile
import unittest
from pathlib import Path

# server.py exits on import when anthropic or reportlab is missing.
# This test only needs the ledger helpers, so stub those imports.
if "anthropic" not in sys.modules:
    sys.modules["anthropic"] = types.ModuleType("anthropic")
if "generate_legal_pdf" not in sys.modules:
    _pdf = types.ModuleType("generate_legal_pdf")
    _pdf.build_pdf = lambda *args, **kwargs: None
    sys.modules["generate_legal_pdf"] = _pdf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402


class _Usage:
    input_tokens = 1_000_000
    output_tokens = 0


class _Response:
    usage = _Usage()


class TestCorruptLedgerFailsClosed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "spend_ledger.json"
        self.ledger.write_text("{not json", encoding="utf-8")
        self._orig_path = server.SPEND_LEDGER
        self._orig_broken = server._LEDGER_BROKEN
        server.SPEND_LEDGER = self.ledger
        server._LEDGER_BROKEN = False

    def tearDown(self):
        server.SPEND_LEDGER = self._orig_path
        server._LEDGER_BROKEN = self._orig_broken
        self.tmp.cleanup()

    def test_corrupt_ledger_is_not_reset_to_zero(self):
        before = self.ledger.read_text(encoding="utf-8")

        server.record_usage(_Response())

        self.assertTrue(server._LEDGER_BROKEN)
        self.assertTrue(server.over_budget())
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), before)
        self.assertNotIn("spent_usd", self.ledger.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
