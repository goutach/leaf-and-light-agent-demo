import re
from pathlib import Path

from .agent import FAQ, route_by_keywords


MODEL_REPO = "Xenova/multilingual-e5-small"
MODEL_REVISION = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
MODEL_DIR = Path("data/semantic")
MODEL_PATH = "onnx/model_quantized.onnx"
MODEL_ASSETS = ("tokenizer.json", MODEL_PATH)
FOLLOWUP = re.compile(r"^(а\s|и\s|тогда\s|а если\s)", re.IGNORECASE)
REFERENCE = re.compile(r"\b(туда|сюда|это|эти|их|ними|тогда|периодичностью)\b", re.IGNORECASE)
SPLIT = re.compile(r"[?;.!]\s+|\s+(?:и|а)\s+(?=(?:кто|как(?:ой|ая|ие)?|сколько|когда|где|что)\b)", re.IGNORECASE)
PARAPHRASES = {
    "minimum": "Можно ли взять только одно дерево? Какое минимальное количество кашпо?",
    "price": "Во сколько обходится аренда одного цветка в месяц? Какой ежемесячный платеж? Что входит в абонплату?",
    "city": "Какая у вас зона обслуживания? Приедете ли за МКАД? Где работает доставка?",
    "delivery": "Через сколько дней кашпо появятся в офисе? Как скоро получим их после подтверждения?",
    "care": "Сотрудникам придется самим ухаживать за грунтом? Как часто приходит специалист и поливает?",
    "replacement": "Что если офисный цветок потеряет вид, зачахнет или перестанет расти? Вы поменяете кашпо?",
    "dark": "Поддерживаете ли кабинеты без естественного освещения? Какой цветок подойдет для тени?",
    "cancel": "Как расторгнуть аренду? За какой срок сообщить, если хотим вернуть растения?",
}
UNSUPPORTED = (
    "Способы оплаты, наличные, оплата картой, залог, рассрочка, склад, самовывоз, забрать заказ самому",
    "Аквариум, рыбки, мебель, офисная техника, товары кроме аренды офисных растений",
    "Размеры и габариты горшка, класс кашпо, материал, фото, сорт или наличие конкретного растения",
    "Гарантии для домашних животных, ядовитость, безопасность, аллергия, здоровье человека",
    "Погода, валютные курсы, расписание автобусов и самолетов, новости, события, прогнозы",
)


class SemanticRouter:
    label = "Локальная нейросеть · multilingual-e5-small"

    def __init__(self, model_dir=MODEL_DIR):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        missing = [asset for asset in MODEL_ASSETS if not (model_dir / asset).is_file()]
        if missing:
            raise ValueError("Сначала загрузите веса командой python -m demo.prepare_model")
        self.np = np
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=256)
        self.tokenizer.enable_padding(pad_id=self.tokenizer.token_to_id("<pad>") or 1)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        self.session = ort.InferenceSession(str(model_dir / MODEL_PATH), options, providers=["CPUExecutionProvider"])
        self.inputs = {node.name for node in self.session.get_inputs()}
        supported = [f"passage: {item['question']} {PARAPHRASES.get(item['id'], '')} {item['answer']}" for item in FAQ]
        unsupported = [f"passage: {topic}" for topic in UNSUPPORTED]
        self.matrix = self.embed(supported + unsupported)

    def embed(self, texts):
        np = self.np
        encoded = self.tokenizer.encode_batch(texts)
        feed = {
            "input_ids": np.asarray([e.ids for e in encoded], dtype=np.int64),
            "attention_mask": np.asarray([e.attention_mask for e in encoded], dtype=np.int64),
            "token_type_ids": np.asarray([e.type_ids for e in encoded], dtype=np.int64),
        }
        hidden = self.session.run(None, {key: value for key, value in feed.items() if key in self.inputs})[0]
        mask = feed["attention_mask"][..., None]
        vectors = (hidden * mask).sum(axis=1) / mask.sum(axis=1).clip(min=1)
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True).clip(min=1e-9)

    def __call__(self, message, previous=""):
        pieces = [part.strip() for part in SPLIT.split(message) if part.strip()][:3]
        expanded = []
        for part in pieces:
            alternatives = [part]
            contextual = FOLLOWUP.search(part) or REFERENCE.search(part)
            if previous and contextual and len(part.split()) <= 9:
                alternatives.append(part + " Контекст: " + previous[:200])
            expanded.append(alternatives)
        vectors = iter(self.embed(["query: " + query for group in expanded for query in group]))
        records = []
        for group in expanded:
            candidates = []
            for index, _ in enumerate(group):
                scores = self.matrix @ next(vectors)
                best, runner_up = scores.argsort()[-2:][::-1]
                candidates.append((float(scores[best]), float(scores[best] - scores[runner_up]), int(best), bool(index)))
            score, margin, match, context_used = candidates[0]
            hint = route_by_keywords(group[0])
            # Уверенная внешняя тема сохраняет приоритет, даже если в прошлом вопросе был подходящий ответ.
            if match >= len(FAQ) and margin >= .012:
                continue
            use_context = score < .84 or margin < .012 or REFERENCE.search(group[0])
            if not hint and use_context and len(candidates) == 2:
                score, margin, match, context_used = candidates[1]
            if match >= len(FAQ):
                continue
            faq_id = FAQ[match]["id"]
            if hint and faq_id != hint:
                continue
            prior_hint = route_by_keywords(previous) if context_used else None
            margin_ok = margin >= .012 or (faq_id == hint and margin >= .003) or (faq_id == prior_hint and score >= .88)
            if score >= .84 and margin_ok:
                if faq_id not in [row["id"] for row in records]:
                    records.append({"id": faq_id, "score": round(score, 3), "context": context_used})
        return records[:2]
