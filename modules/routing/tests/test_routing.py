import unittest
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from postcode.routing import Assistant


class RoutingTests(unittest.TestCase):
    def setUp(self):
        texts = ['пароль вход кабинет', 'вход пароль ошибка', 'доставка посылка задержка', 'посылка доставка адрес']
        self.rows = [dict(id=str(i),text=t,line=str(i//2),category='категория',result='итог',group=str(i//2),source_row=i+2,original={'Описание 2':t}) for i,t in enumerate(texts)]
        vectorizer = TfidfVectorizer()
        matrix = vectorizer.fit_transform(texts)
        classifier = LogisticRegression().fit(matrix,[r['line'] for r in self.rows])
        self.model = Assistant(vectorizer,classifier,self.rows,.5)

    def test_self_and_duplicate_groups(self):
        result = self.model.find_similar('пароль вход кабинет',exclude_id='0',min_similarity=0)
        self.assertNotIn('0',[r['id'] for r in result])
        self.assertEqual(len(result),1)
        self.assertEqual(self.model.find_similar('пароль вход кабинет',exclude_group='0'),[])

    def test_unknown_and_empty_input(self):
        for text in ['', 'абракадабра']:
            self.assertTrue(self.model.recommend_line(text)['needs_review'])
            self.assertIsNone(self.model.recommend_line(text)['line'])
            self.assertEqual(self.model.find_similar(text),[])

    def test_original_record_and_evidence(self):
        hit = self.model.find_similar('пароль вход')[0]
        self.assertEqual(hit['original']['Описание 2'],hit['text'])
        answer = self.model.recommend_line('пароль вход')
        self.assertTrue(answer['evidence'])
        self.assertTrue(all(e['contribution']>0 for e in answer['evidence']))
        self.assertTrue(answer['needs_review'])  # rare training classes are never auto-routed


if __name__ == '__main__': unittest.main()
