"""Prepare the supplied appeal CSV for text classification, preserving source data."""
import argparse
import csv
import hashlib
import json
import random
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path


def normalize(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def prepare(source, output, encoding='cp1251'):
    original = source.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    with source.open(encoding=encoding, newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        rows = list(reader)
    assert fields and len(fields) == len(set(fields))
    assert all(None not in row and None not in row.values() for row in rows)
    assert len({r['Номер запроса'] for r in rows}) == len(rows)
    from postcode.audit_csv import near_pairs
    texts = [normalize(r['Описание 2']).casefold() for r in rows]
    parents = list(range(len(rows)))
    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    for a, b, _ in near_pairs(texts):
        parents[root(b)] = root(a)
    component_keys = {}
    for i, text in enumerate(texts):
        key = root(i)
        component_keys[key] = min(component_keys.get(key, text), text)
    groups = defaultdict(list)
    for number, row in enumerate(rows, 2):
        text = normalize(row['Описание 2'])
        key = component_keys[root(number - 2)]
        group = hashlib.sha256(key.encode()).hexdigest()
        row.update(source_record=number, text_clean=text, duplicate_group=group)
        groups[group].append(row)
    frequencies = Counter(r['Вид запроса'] for r in rows)
    top15 = {name for name, _ in sorted(frequencies.items(), key=lambda x: (-x[1], x[0]))[:15]}
    eligible = defaultdict(list)
    for group, members in groups.items():
        conflict = len({r['Вид запроса'] for r in members}) > 1
        for r in members:
            issues = []
            if conflict:
                issues.append('conflicting_category')
            if len(members) > 1:
                issues.append('repeated_text')
            if len(r['text_clean']) < 30:
                issues.append('short_text_review')
            placeholder = r['text_clean'].casefold() in {'фио: , комментарий:', 'фио: тест, комментарий: тест'}
            if placeholder:
                issues.append('empty_or_test_template')
            if r['Услуга'] == '---)':
                issues.append('unknown_service')
            if not r['Компонент услуги 1 уровня'].strip():
                issues.append('missing_service_component')
            r.update(quality_flags=';'.join(issues), split='review' if conflict or placeholder else '',
                     is_top15=int(r['Вид запроса'] in top15))
        if not conflict and members[0]['split'] != 'review':
            eligible[members[0]['Вид запроса']].append(group)
    rng = random.Random(42)
    for category in sorted(eligible):
        keys = sorted(eligible[category])
        rng.shuffle(keys)
        # Class-aware allocation by independent text groups, not individual rows.
        count = max(1, round(len(keys) * .15)) if len(keys) >= 3 else 0
        for i, group in enumerate(keys):
            split = 'test' if i < count else 'validation' if i < 2 * count else 'train'
            for row in groups[group]:
                row['split'] = split
    output.mkdir(parents=True, exist_ok=True)
    extra = ['source_record', 'text_clean', 'duplicate_group', 'quality_flags', 'split', 'is_top15']
    write_csv(output / 'appeals_prepared.csv', fields + extra, rows)
    write_csv(output / 'review.csv', fields + extra, [r for r in rows if r['split'] == 'review'])
    model_fields = ['request_id', 'text', 'category', 'duplicate_group', 'source_record', 'is_top15']
    for split in ['train', 'validation', 'test']:
        subset = [dict(request_id=r['Номер запроса'], text=r['text_clean'], category=r['Вид запроса'],
                       duplicate_group=r['duplicate_group'], source_record=r['source_record'],
                       is_top15=r['is_top15']) for r in rows if r['split'] == split]
        write_csv(output / f'{split}.csv', model_fields, subset)
    coverage = []
    for category, total in frequencies.most_common():
        subset = [r for r in rows if r['Вид запроса'] == category]
        coverage.append(dict(category=category, total=total,
                            independent_groups=len({r['duplicate_group'] for r in subset}),
                            **{s: sum(r['split'] == s for r in subset) for s in ['train', 'validation', 'test', 'review']}))
    write_csv(output / 'class_coverage.csv', list(coverage[0]), coverage)
    counts = Counter(r['split'] for r in rows)
    summary = dict(source=str(source.resolve()), source_sha256=digest, rows=len(rows),
                   categories=len(frequencies), split_counts=dict(counts), seed=42,
                   grouping='token-set Jaccard >= 0.85, connected components',
                   duplicate_groups=sum(len(v) > 1 for v in groups.values()),
                   conflicting_groups=sum(len({r['Вид запроса'] for r in v}) > 1 for v in groups.values()),
                   normalized_text_changed=sum(r['Описание 2'] != r['text_clean'] for r in rows),
                   flags=dict(Counter(flag for r in rows for flag in r['quality_flags'].split(';') if flag)))
    # Round-trip checks: source values preserved, records accounted for, groups disjoint.
    with (output / 'appeals_prepared.csv').open(encoding='utf-8-sig', newline='') as f:
        saved = list(csv.DictReader(f))
    assert len(saved) == len(rows)
    assert all(all(a[c] == b[c] for c in fields) for a, b in zip(rows, saved))
    assert all(len({r['split'] for r in members}) == 1 for members in groups.values())
    train_labels = {r['Вид запроса'] for r in rows if r['split'] == 'train'}
    assert all(r['Вид запроса'] in train_labels for r in rows if r['split'] in {'validation', 'test'})
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    notes = ['# Подготовка датасета для классификации', '',
             f'Источник: `{source.name}`. SHA-256: `{digest}`.', '',
             f'Сохранены все {len(rows)} записей и все 34 исходных поля без изменения значений.',
             'Кодировка выходных CSV — UTF-8 с BOM, разделитель — запятая.', '',
             '## Результат', '', *[f'- {s}: {counts[s]} записей.' for s in ['train', 'validation', 'test', 'review']], '',
             '## Правила', '',
             '- text_clean: Unicode NFKC, единичные пробелы, удаление пробелов по краям; регистр текста сохранён.',
             '- Группы повторов: связные компоненты пар с Jaccard множеств слов >= 0.85. Все записи группы относятся к одной части.',
             '- Противоречивые категории и два явно пустых/тестовых шаблона отложены в review.csv; метки не исправлены автоматически.',
             '- Короткие тексты отмечены, но не исключены автоматически. Пропуски не заполнены догадками.',
             '- Разделение приблизительно 70/15/15 по группам внутри класса, seed=42. Классы с менее чем тремя допустимыми группами остаются в train.',
             '- class_coverage.csv показывает фактическое покрытие каждого класса.',
             '- Все 43 категории сохранены; is_top15 отмечает 15 самых частых категорий исходной истории.',
             '- Вход модели — только text; category — цель. ID, группы, source_record и is_top15 — служебные поля, не признаки.', '',
             '## Ограничения', '',
             '- Группировка защищает от лексически близких повторов при пороге 0.85, но не гарантирует обнаружение смысловых перефразировок. Общие шаблоны могут объединять разные обращения.',
             '- Разделение не временное; оценка на нём не доказывает качество на будущих периодах.',
             '- Полные тексты не обезличивались дополнительно. Подготовка выполнялась локально.',
             '- Сведения из будущего обработки не включены в модельные CSV. Доступность всего текста на момент регистрации требует подтверждения.',
             '- SLA и исходная разметка сохранены; спорные данные требуют ручной проверки.',
             '- Модель пока не обучена. TF-IDF следует обучать только на train; настройки выбирать на validation; test оставить для финальной оценки.', '',
             '## Проверки', '',
             'Проверены сохранность исходных полей после повторного чтения CSV, число записей, уникальность ID, отсутствие пересечений групп и наличие проверочных категорий в train. Хеш исходного файла не изменился.', '']
    (output / 'README.md').write_text('\n'.join(notes), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, default=Path('reports/classification_prepared'))
    args = parser.parse_args()
    prepare(args.source, args.output)
