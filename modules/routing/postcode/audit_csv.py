"""Reproducible local CSV audit; reports contain row references, not appeal text."""
import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from postcode.prepare_csv import normalize


def duration(value):
    if not value.strip():
        return None
    if not re.fullmatch(r'\d+:[0-5]\d:[0-5]\d', value):
        raise ValueError(value)
    h, m, s = map(int, value.split(':'))
    return h * 3600 + m * 60 + s


def near_pairs(texts, threshold=.85):
    """Exact token-set Jaccard search, including all pairs above threshold.

    This detects lexical similarity, not semantic equivalence. No labels used.
    """
    if not 0 < threshold <= 1:
        raise ValueError('threshold must be in (0, 1]')
    tokens = [set(re.findall(r'\w+', normalize(t).casefold())) for t in texts]
    index = defaultdict(list)
    pairs = []
    for i, current in enumerate(tokens):
        candidates = Counter(j for token in current for j in index[token])
        for j, overlap in candidates.items():
            score = overlap / (len(current) + len(tokens[j]) - overlap)
            if score >= threshold:
                pairs.append((j, i, round(score, 6)))
        for token in current:
            index[token].append(i)
    return pairs


def audit(source, output):
    with source.open(encoding='cp1251', newline='') as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        rows = list(reader)
    if not fields or len(set(fields)) != len(fields) or any(None in r or None in r.values() for r in rows):
        raise ValueError('Malformed CSV schema or row')
    required = ['Номер запроса', 'Описание 2', 'Вид запроса', 'Кем решен (группа)',
                'Дата регистрации', 'Фактическое время выполнения', 'Просрочен?*',
                'Норматив ное время обработки (SLA)', 'Фактическая длительность выполнения запроса (SLA)']
    if set(required) - set(fields):
        raise ValueError(f'Missing columns: {set(required) - set(fields)}')
    categories = Counter(r['Вид запроса'] for r in rows)
    date_columns = {f: '%d.%m.%Y %H:%M:%S' if f == 'Дата регистрации' else '%d.%m.%y %H:%M'
                    for f in fields if f in ['Дата регистрации', 'Плановое время выполнения',
                    'Крайний срок обработки', 'Фактическое время выполнения', 'Время входа в статус', 'Дата последнего изменения']}
    dates, invalid, negative, mismatches = {}, [], [], []
    for f, fmt in date_columns.items():
        parsed = []
        for i, row in enumerate(rows, 2):
            try:
                parsed.append(datetime.strptime(row[f], fmt) if row[f].strip() else None)
            except ValueError:
                invalid.append({'record': i, 'field': f})
                parsed.append(None)
        dates[f] = parsed
    for i, row in enumerate(rows):
        start, end = dates['Дата регистрации'][i], dates['Фактическое время выполнения'][i]
        if start and end and end < start:
            negative.append({'record': i + 2, 'seconds': (end-start).total_seconds()})
        values = []
        for field in required[-2:]:
            try:
                values.append(duration(row[field]))
            except ValueError:
                invalid.append({'record': i + 2, 'field': field})
                values.append(None)
        norm, actual = values
        flag = row['Просрочен?*']
        if norm is not None and actual is not None and flag in {'Просрочен', 'Не просрочен'}:
            if (actual > norm) != (flag == 'Просрочен'):
                mismatches.append(i + 2)
    pairs = near_pairs([r['Описание 2'] for r in rows])
    result = dict(source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  rows=len(rows), columns=len(fields), categories=len(categories),
                  unique_ids=len({r['Номер запроса'] for r in rows}),
                  top15=sorted(categories.items(), key=lambda x: (-x[1], x[0]))[:15],
                  solving_lines=dict(Counter(r['Кем решен (группа)'] for r in rows)),
                  missing={f: sum(not r[f].strip() for r in rows) for f in fields},
                  sla_flags=dict(Counter(r['Просрочен?*'] for r in rows)),
                  invalid_values=invalid, negative_intervals=negative, sla_duration_disagreements=mismatches,
                  dates={f: {'min': min(v for v in vs if v).isoformat() if any(vs) else None,
                             'max': max(v for v in vs if v).isoformat() if any(vs) else None} for f, vs in dates.items()},
                  near_threshold=.85, near_pairs=len(pairs),
                  near_conflicting_pairs=sum(rows[a]['Вид запроса'] != rows[b]['Вид запроса'] for a,b,_ in pairs))
    output.mkdir(parents=True, exist_ok=True)
    (output/'csv_audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    with (output/'near_duplicates.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['source_record_a', 'source_record_b', 'jaccard', 'category_conflict'])
        writer.writerows((a+2,b+2,s,rows[a]['Вид запроса'] != rows[b]['Вид запроса']) for a,b,s in pairs)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, default=Path('reports'))
    args = parser.parse_args()
    audit(args.source, args.output)
