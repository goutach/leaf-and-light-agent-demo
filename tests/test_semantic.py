import unittest

from demo.semantic import MODEL_ASSETS, MODEL_DIR, SemanticRouter


@unittest.skipUnless(all((MODEL_DIR / asset).is_file() for asset in MODEL_ASSETS), "Сначала загрузите веса нейросети")
class LiveSemanticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.route = SemanticRouter()

    def ids(self, question, prior=""):
        return [item["id"] for item in self.route(question, prior)]

    def test_paraphrase_and_two_sources(self):
        self.assertEqual(self.ids("Сотрудникам придется самим следить за грунтом?"), ["care"])
        self.assertEqual(self.ids("Через сколько дней кашпо окажутся в офисе?"), ["delivery"])
        self.assertEqual(self.ids("Сколько у вас стоит одно растение в месяц?"), ["price"])
        self.assertEqual(self.ids("А можно только одно растение?"), ["minimum"])
        self.assertEqual(self.ids("Сколько будет стоить аренда и как часто приходит специалист?"), ["price", "care"])

    def test_context(self):
        cases = [
            ("А что туда входит?", "Сколько стоит аренда растений в месяц?", "price"),
            ("А как часто?", "Кто отвечает за полив растений?", "care"),
            ("С какой периодичностью?", "Сотрудникам придется самим следить за грунтом?", "care"),
        ]
        for question, prior, expected in cases:
            with self.subTest(question=question):
                selected = self.route(question, prior)
                self.assertEqual([row["id"] for row in selected], [expected])
                self.assertTrue(selected[0]["context"])

    def test_reject_unsupported_even_with_related_context(self):
        for question in ("Могу забрать растения наличными из Казани?", "А можно внести предоплату картой?",
                         "Можно заказать аквариум с рыбками?", "Расписание самолетов из Москвы?"):
            with self.subTest(question=question):
                self.assertEqual(self.ids(question, "Сколько стоит аренда?"), [])
        for question in ("А можно наличными?", "Они опасны для кошек?"):
            with self.subTest(question=question):
                self.assertEqual(self.ids(question, "Кто отвечает за полив растений?"), [])
