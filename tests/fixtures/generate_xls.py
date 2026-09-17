"""Regenerate the synthetic BIFF8 fixture with xlwt 1.3.0 (not a runtime dependency)."""
from pathlib import Path

import xlwt

book = xlwt.Workbook()
data = book.add_sheet("Data")
rows = [
    ["Код", "NA", "NULL", "Empty", "_customer", "*custom", "Number", "Boolean"],
    ["00123", "NA", "NULL", None, "Иван", "звезда", 0, False],
    ["00456", "NA", "NULL", None, "Анна", "has_value", 2, True],
]
for row_index, values in enumerate(rows):
    for column, value in enumerate(values):
        if value is not None:
            data.write(row_index, column, value)
book.add_sheet("Empty")
second = book.add_sheet("Second")
second.write(0, 0, "A")
second.write(1, 0, "another")
book.save(str(Path(__file__).with_name("values.xls")))
