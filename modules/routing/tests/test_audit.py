import unittest

from postcode.audit import profile


class AuditTests(unittest.TestCase):
    def test_missing_zero_duplicates_and_categories(self):
        names = ["text", "category", "count"]
        rows = [(2, dict(zip(names, ["  Ошибка  Входа", "A", 0]))),
                (3, dict(zip(names, ["ошибка входа", "B", None]))),
                (4, dict(zip(names, ["", "A", 1]))),
                (5, dict(zip(names, ["  Ошибка  Входа", "A", 0])))]
        result = profile(names, rows, "category", ["text"])
        self.assertEqual(result["columns"]["count"]["missing"], 1)
        self.assertEqual(result["exact_duplicate_groups"], [[2, 5]])
        self.assertEqual(result["normalized_text_duplicate_groups"], [[2, 3, 5]])
        self.assertEqual(result["top_15"][0], {"category": "A", "count": 3})

    def test_unknown_column_is_rejected(self):
        with self.assertRaises(ValueError):
            profile(["text"], [(2, {"text": "abc"})], "missing")

    def test_top_15_and_empty_text(self):
        rows = [(i + 2, {"category": str(i), "text": None}) for i in range(17)]
        result = profile(["category", "text"], rows, "category", ["text"])
        self.assertEqual(len(result["top_15"]), 15)
        self.assertEqual(result["outside_top_15"], 2)
        self.assertEqual(result["normalized_text_duplicate_groups"], [])


if __name__ == "__main__":
    unittest.main()
