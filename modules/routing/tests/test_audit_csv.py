import unittest
from postcode.audit_csv import duration, near_pairs


class CsvAuditTests(unittest.TestCase):
    def test_duration_above_day_and_missing(self):
        self.assertEqual(duration('60:15:09'), 216909)
        self.assertIsNone(duration(''))
        with self.assertRaises(ValueError):
            duration('12:65:00')

    def test_near_duplicates_ignore_case_and_word_order(self):
        pairs = near_pairs(['Ошибка входа портал', 'ПОРТАЛ ошибка входа', 'другая проблема', ''])
        self.assertEqual(pairs, [(0, 1, 1.0)])

    def test_threshold_boundary(self):
        self.assertEqual(near_pairs(['a b c d', 'a b c d e'], .8), [(0, 1, .8)])
        self.assertEqual(near_pairs(['a b c d', 'a b c d e'], .81), [])
