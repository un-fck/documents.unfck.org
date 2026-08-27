import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

PYTHON_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PYTHON_DIR))
spec = importlib.util.spec_from_file_location("fulltext_metrics", PYTHON_DIR / "fulltext_metrics.py")
metrics = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = metrics
assert spec.loader
spec.loader.exec_module(metrics)


class FulltextMetricsTests(unittest.TestCase):
    def test_unicode_numbers_one_letter_and_apostrophe_are_words(self):
        self.assertEqual(
            metrics.normalize_tokens("A 2030 café l’ONU _ignored_"),
            ["a", "2030", "café", "l'onu", "ignored"],
        )

    def test_profile_includes_annex_and_table_but_excludes_boilerplate(self):
        rows = [
            metrics.SemanticElement("frontmatter", "United Nations A/RES/1"),
            metrics.SemanticElement("title", "A shared future"),
            metrics.SemanticElement("opening", "The General Assembly"),
            metrics.SemanticElement("heading", "Annex I"),
            metrics.SemanticElement("paragraph", "Decides 1 thing"),
            metrics.SemanticElement("table", "Goal | 2 targets"),
            metrics.SemanticElement("footnote", "Do not count this"),
            metrics.SemanticElement("vote_record", "In favour 100"),
            metrics.SemanticElement("signature", "President"),
        ]
        result = metrics.build_metric(rows)
        self.assertIsNotNone(result)
        self.assertEqual(result.word_count, 14)
        self.assertIn("annex i decides 1 thing goal 2 targets", result.token_text)
        self.assertNotIn("united nations", result.token_text)
        self.assertNotIn("favour", result.token_text)

    def test_hash_is_stable_under_unicode_compatibility_and_case(self):
        a = metrics.build_metric([metrics.SemanticElement("paragraph", "Ｆuture CAFÉ")])
        b = metrics.build_metric([metrics.SemanticElement("paragraph", "Future café")])
        self.assertEqual(a.content_sha256, b.content_sha256)

    def test_empty_non_substantive_document_is_omitted(self):
        self.assertIsNone(metrics.build_metric([
            metrics.SemanticElement("frontmatter", "masthead"),
            metrics.SemanticElement("footnote", "note"),
        ]))

    def test_manifest_is_exact_deduplicated_and_comment_aware(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "symbols.txt"
            path.write_text("s/res/2\n# note\nA/RES/1\ns/res/2\n", encoding="utf-8")
            self.assertEqual(metrics.read_symbols_file(path), ["A/RES/1", "S/RES/2"])


if __name__ == "__main__":
    unittest.main()
