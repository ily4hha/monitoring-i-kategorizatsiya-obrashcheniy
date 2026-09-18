"""Train-only routing and retrieval. CLI: python -m postcode.routing --help."""
import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import joblib
import numpy as np
from openpyxl import load_workbook
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score

from postcode.prepare_csv import normalize, prepare, write_csv

ID, TEXT, LINE, CATEGORY, RESULT = 'Номер запроса', 'Описание 2', 'Кем решен (группа)', 'Вид запроса', 'Результат работ'


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def prepare_xlsx(source, output):
    output.mkdir(parents=True, exist_ok=True)
    wb = load_workbook(source, read_only=True, data_only=True)
    values = iter(wb['Sheet0'].values)
    header = next(values)
    fields = [h for h in header if h is not None]
    if len(set(fields)) != len(fields):
        raise ValueError('Duplicate column labels')
    rows = []
    for number, values_row in enumerate(values, 2):
        if values_row[0] is None:
            raise ValueError(f'Missing ID at Excel row {number}')
        row = {h: '' if v is None else str(v) for h, v in zip(header, values_row) if h is not None}
        rows.append(row)
    wb.close()
    comparison = {'xlsx_rows': len(rows)}
    previous = Path('data/raw/Обращения_1931.csv')
    if previous.exists():
        with previous.open(encoding='cp1251', newline='') as stream:
            old = {r[ID]: r for r in csv.DictReader(stream)}
        comparison['csv_rows'] = len(old)
        comparison['shared_ids'] = len({r[ID] for r in rows} & set(old))
        comparison['different_fields_by_id'] = {f: sum(r[ID] not in old or r[f] != old[r[ID]][f] for r in rows) for f in [TEXT, LINE, CATEGORY, RESULT]}
        comparison['normalized_text_differences'] = sum(r[ID] not in old or normalize(r[TEXT]) != normalize(old[r[ID]][TEXT]) for r in rows)
    # Existing grouping/split policy reused; source values retained in JSON below.
    intermediate = output / 'source_utf8.csv'
    with intermediate.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    prepare(intermediate, output / 'shared_split', encoding='utf-8-sig')
    with (output / 'shared_split/appeals_prepared.csv').open(encoding='utf-8-sig', newline='') as stream:
        prepared = {r[ID]: r for r in csv.DictReader(stream)}
    records = []
    for n, row in enumerate(rows, 2):
        p = prepared[row[ID]]
        if normalize(row[TEXT]) != p['text_clean']:
            raise ValueError('Text not representable in shared CSV preparation; use Unicode pipeline')
        records.append(dict(id=row[ID], text=normalize(row[TEXT]), line=row[LINE], category=row[CATEGORY],
                            result=row[RESULT], split=p['split'], group=p['duplicate_group'], source_row=n, original=row))
    dump(output / 'records.json', records)
    manifest = dict(source=str(source.resolve()), sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                    sheet='Sheet0', comparison=comparison, splits=dict(Counter(r['split'] for r in records)),
                    lines=dict(Counter(r['line'] for r in records)),
                    coverage={s: dict(Counter(r['line'] for r in records if r['split'] == s)) for s in ['train','validation','test','review']})
    dump(output / 'manifest.json', manifest)
    return records, manifest


class Assistant:
    def __init__(self, vectorizer, classifier, history, threshold):
        self.vectorizer, self.classifier, self.history, self.threshold = vectorizer, classifier, history, threshold
        self.matrix = vectorizer.transform([r['text'] for r in history])
        self.counts = Counter(r['line'] for r in history)

    @classmethod
    def load(cls, path):
        """Load only a trusted, locally produced joblib file."""
        return joblib.load(path)

    def recommend_line(self, text):
        text = normalize(text)
        x = self.vectorizer.transform([text])
        if not text or not x.nnz:
            return dict(line=None, confidence=0., needs_review=True, explanation='Нет известных модели слов; требуется ручной разбор.', evidence=[])
        scores = self.classifier.predict_proba(x)[0]
        index = int(scores.argmax())
        line = str(self.classifier.classes_[index])
        weights = self.classifier.coef_[index] if len(scores) > 2 else self.classifier.coef_[0] * (1 if index else -1)
        contributions = x.data * weights[x.indices]
        vocabulary = self.vectorizer.get_feature_names_out()
        evidence = [{'term': str(vocabulary[x.indices[i]]), 'contribution': float(contributions[i])}
                    for i in np.argsort(-contributions)[:5] if contributions[i] > 0]
        review = float(scores[index]) < self.threshold or self.counts[line] < 10
        return dict(line=line, confidence=float(scores[index]), needs_review=review,
                    confidence_kind='Некалиброванная вероятностная оценка логистической регрессии; не гарантия правильности.',
                    explanation='Слова с положительным вкладом в оценку линии: ' + ', '.join(e['term'] for e in evidence),
                    evidence=evidence, probabilities={str(c):float(v) for c,v in zip(self.classifier.classes_,scores)})

    def find_similar(self, text, exclude_id=None, exclude_group=None, limit=5, min_similarity=.1):
        if limit < 1 or not 0 <= min_similarity <= 1:
            raise ValueError('limit >= 1; min_similarity in [0,1]')
        x = self.vectorizer.transform([normalize(text)])
        scores = (self.matrix @ x.T).toarray().ravel()
        results, seen = [], set()
        for i in np.argsort(-scores, kind='stable'):
            row = self.history[int(i)]
            if scores[i] <= 0 or scores[i] < min_similarity:
                break
            if str(row['id']) == str(exclude_id) or row['group'] == exclude_group or row['group'] in seen:
                continue
            seen.add(row['group'])
            results.append({k:row[k] for k in ['id','text','category','line','result','source_row']})
            results[-1].update(similarity=float(scores[i]), similarity_kind='Косинусное сходство TF-IDF, не вероятность',
                               source_sheet='Sheet0', original=row['original'])
            if len(results) == limit:
                break
        return results


def evaluate(model, rows):
    actual = [r['line'] for r in rows]
    answers = [model.recommend_line(r['text']) for r in rows]
    predictions = [a['line'] or 'Нет рекомендации' for a in answers]
    labels = sorted(set(model.classifier.classes_) | set(actual))
    accepted = [i for i,a in enumerate(answers) if not a['needs_review']]
    return dict(n=len(rows), accuracy=accuracy_score(actual,predictions),
                macro_f1_supported=f1_score(actual,predictions,labels=sorted(set(actual)),average='macro',zero_division=0),
                report=classification_report(actual,predictions,labels=labels,output_dict=True,zero_division=0),
                confusion_labels=labels+['Нет рекомендации'],
                confusion_matrix=confusion_matrix(actual,predictions,labels=labels+['Нет рекомендации']).tolist(),
                automatic_coverage=len(accepted)/len(rows),
                accepted_accuracy=accuracy_score([actual[i] for i in accepted],[predictions[i] for i in accepted]) if accepted else None)


def train(source, output):
    rows, manifest = prepare_xlsx(source, output)
    subsets = {s:[r for r in rows if r['split']==s] for s in ['train','validation','test']}
    for a,b in [('train','validation'),('train','test'),('validation','test')]:
        assert not ({r['group'] for r in subsets[a]} & {r['group'] for r in subsets[b]})
    fit, val, test = subsets['train'], subsets['validation'], subsets['test']
    vec = TfidfVectorizer(lowercase=True, ngram_range=(1,2), min_df=2, sublinear_tf=True, max_features=40000)
    x = vec.fit_transform([r['text'] for r in fit])
    candidates = []
    for balanced in [None, 'balanced']:
        clf = LogisticRegression(C=4., class_weight=balanced, max_iter=1500, random_state=42)
        clf.fit(x,[r['line'] for r in fit])
        model = Assistant(vec,clf,fit,1.01)
        score = evaluate(model,val)['macro_f1_supported']
        candidates.append((score,model,balanced))
    _,model,balanced = max(candidates,key=lambda item:item[0])
    # Abstention threshold selected only on validation: >=85% accepted accuracy, >=20 cases.
    thresholds = []
    for threshold in np.arange(.35,.96,.05):
        model.threshold = float(threshold)
        stats = evaluate(model,val)
        if stats['accepted_accuracy'] is not None and stats['accepted_accuracy'] >= .85 and stats['automatic_coverage']*len(val) >= 20:
            thresholds.append((stats['automatic_coverage'],float(threshold)))
    model.threshold = max(thresholds)[1] if thresholds else 1.01
    joblib.dump(model,output / 'assistant.joblib')
    majority = Counter(r['line'] for r in fit).most_common(1)[0][0]
    metrics = dict(configuration=dict(class_weight=balanced,C=4,threshold=model.threshold,seed=42),
                   candidates=[dict(class_weight=b,validation_macro_f1=s) for s,_,b in candidates],
                   validation=evaluate(model,val),test=evaluate(model,test),
                   test_majority_accuracy=sum(r['line']==majority for r in test)/len(test))
    retrieval, annotations, examples = [], [], {'routing_success':[], 'routing_failure':[], 'routing_review':[]}
    for row in test:
        answer = model.recommend_line(row['text'])
        key = 'routing_success' if answer['line']==row['line'] else 'routing_failure'
        example = dict(id=row['id'],text=row['text'],expected=row['line'],prediction=answer)
        if len(examples[key])<4: examples[key].append(example)
        if answer['needs_review'] and len(examples['routing_review'])<4: examples['routing_review'].append(example)
        found = model.find_similar(row['text'],exclude_id=row['id'],exclude_group=row['group'])
        retrieval.append(dict(id=row['id'],hits=len(found),category_hit=any(r['category']==row['category'] for r in found),line_hit=any(r['line']==row['line'] for r in found)))
    # Deterministic 30-query review sample; no cherry-picking by search outcome.
    sample = sorted(test,key=lambda r:hashlib.sha256(r['id'].encode()).hexdigest())[:30]
    for row in sample:
        found = model.find_similar(row['text'],exclude_id=row['id'],exclude_group=row['group'])
        for rank,r in enumerate(found,1):
            annotations.append(dict(query_id=row['id'],query_text=row['text'],rank=rank,result_id=r['id'],result_text=r['text'],
                                    result_category=r['category'],result_line=r['line'],result_outcome=r['result'],similarity=r['similarity'],
                                    relevant='',useful='',comment=''))
        if not found:
            annotations.append(dict(query_id=row['id'],query_text=row['text'],rank=0,result_id='',result_text='',result_category='',result_line='',result_outcome='',similarity=0,relevant='',useful='',comment='Нет результатов'))
    metrics['retrieval'] = dict(queries=len(test),category_hit_at_5=sum(r['category_hit'] for r in retrieval)/len(test),
                                line_hit_at_5=sum(r['line_hit'] for r in retrieval)/len(test),
                                no_results=sum(r['hits']==0 for r in retrieval),
                                warning='Совпадение меток — только косвенная метрика. Полезность требует оценки содержимого.')
    dump(output/'metrics.json',metrics)
    dump(output/'examples.json',examples)
    dump(output/'retrieval_checks.json',retrieval)
    write_csv(output/'search_review.csv',list(annotations[0]),annotations)
    print(json.dumps(dict(manifest=manifest,metrics=metrics),ensure_ascii=False,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('train'); p.add_argument('source',type=Path); p.add_argument('--output',type=Path,default=Path('reports/routing'))
    p=commands.add_parser('query'); p.add_argument('text'); p.add_argument('--model',type=Path,default=Path('reports/routing/assistant.joblib')); p.add_argument('--exclude-id')
    args=parser.parse_args()
    if args.command=='train': train(args.source,args.output)
    else:
        model=Assistant.load(args.model)
        print(json.dumps(dict(routing=model.recommend_line(args.text),similar=model.find_similar(args.text,exclude_id=args.exclude_id)),ensure_ascii=False,indent=2))


if __name__=='__main__':
    from postcode.routing import main as entrypoint
    entrypoint()
