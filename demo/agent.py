import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


FAQ = json.loads(Path(__file__).with_name("faq.json").read_text(encoding="utf-8"))
UNKNOWN = "В базе знаний нет ответа на этот вопрос. Если хотите, оставьте заявку, и специалист уточнит детали."
PHONE = re.compile(r"(?<!\d)(?:\+7|8)[\s(\-]*\d{3}[\s)\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
NAME = re.compile(r"[а-яёa-z][а-яёa-z-]{1,39}(?:\s+[а-яёa-z][а-яёa-z-]{1,39}){0,2}", re.IGNORECASE)
NAMED = re.compile(r"(?:меня зовут\s+|имя\s*[:—-]\s*)([\w -]{2,70})", re.IGNORECASE)
CONTACT = re.compile(r"((?:остав|оформ)\w*\s+заявк|свяжитесь со мной|хочу заказать|перезвоните мне)", re.IGNORECASE)


def route_by_keywords(question):
    text = question.casefold().replace("ё", "е")
    scored = []
    for item in FAQ:
        matches = [word for word in item["keywords"] if re.search(r"\b" + re.escape(word), text)]
        if matches:
            scored.append((sum(3 if " " in word else 1 for word in matches), item["id"]))
    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


class NoModelRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class ModelRouter:
    """Модель выбирает ID. Пользователю возвращается исходная запись справочника."""

    def __init__(self, base, model, token):
        url = urlsplit(base)
        local = url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost"}
        if not (url.hostname and (url.scheme == "https" or local)) or url.username or url.query or url.fragment:
            raise ValueError("Адрес модели должен использовать HTTPS или локальный HTTP")
        self.base, self.model, self.token = base.rstrip("/"), model, token
        self.http = build_opener(NoModelRedirect())

    def __call__(self, question):
        options = [{"id": row["id"], "question": row["question"]} for row in FAQ]
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "Выбери ID одного вопроса, который отвечает на запрос. Для неизвестного или неоднозначного запроса верни null. Ответ только один JSON: {\"id\":\"...\"} либо {\"id\":null}. Справочник: " + json.dumps(options, ensure_ascii=False)},
                {"role": "user", "content": question},
            ],
            "max_tokens": 100,
        }
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = Request(self.base + "/chat/completions", data=json.dumps(payload).encode(), headers=headers, method="POST")
        with self.http.open(request, timeout=7) as response:
            data = json.loads(response.read(128 * 1024))
        return json.loads(data["choices"][0]["message"]["content"])["id"]


def configured_router():
    model = os.getenv("DEMO_MODEL")
    if not model:
        return None
    return ModelRouter(os.getenv("DEMO_MODEL_BASE", "https://api.openai.com/v1"), model, os.getenv("DEMO_MODEL_TOKEN"))


@dataclass
class Contact:
    name: str = ""
    phone: str = ""


class Agent:
    def __init__(self, crm, router=None):
        self.crm = crm
        self.router = router
        self.faq = {row["id"]: row for row in FAQ}
        self.contacts = {}
        self.lock = threading.RLock()

    def chat(self, session, message):
        message = message.strip()
        number = PHONE.search(message)
        named = NAMED.search(message)
        with self.lock:
            if session in self.contacts or CONTACT.search(message) or number or named:
                return self._contact(session, message, number, named)

        match = None
        if self.router:
            try:
                match = self.router(message)
            except (URLError, TimeoutError, ValueError, KeyError, TypeError, IndexError):
                match = route_by_keywords(message)
        else:
            match = route_by_keywords(message)
        item = self.faq.get(match) if isinstance(match, str) else None
        if item:
            return {"kind": "faq", "text": item["answer"], "source": item["question"]}
        return {"kind": "unknown", "text": UNKNOWN}

    def _contact(self, session, message, number, named):
        if message.casefold() in {"отмена", "отменить заявку"}:
            self.contacts.pop(session, None)
            return {"kind": "contact", "text": "Остановил заполнение заявки."}
        contact = self.contacts.setdefault(session, Contact())
        if number:
            digits = re.sub(r"\D", "", number.group())
            contact.phone = "+7" + digits[-10:]
        candidate = named.group(1) if named else (message[:number.start()] if number else message)
        candidate = re.sub(r"[, .]*(?:мой\s+)?(?:телефон|номер)\s*[:—-]?\s*$", "", candidate, flags=re.IGNORECASE)
        candidate = candidate.strip(" ,.!—-:")
        if not contact.name and NAME.fullmatch(candidate) and not CONTACT.search(candidate):
            contact.name = candidate.title() if candidate.islower() else candidate
        if not contact.name or not contact.phone:
            if contact.name:
                prompt = "Укажите телефон, например +7 900 000-00-01."
            elif contact.phone:
                prompt = "Как к вам обращаться? Укажите имя."
            else:
                prompt = "Напишите ваше имя и телефон. Можно одним сообщением, например Анна +7 900 000-00-01."
            return {"kind": "contact", "text": prompt}
        request_id = hashlib.sha256(f"{session}\n{contact.name.casefold()}\n{contact.phone}".encode()).hexdigest()
        try:
            lead_id = self.crm.create(request_id, contact.name, contact.phone)["id"]
        except (URLError, TimeoutError, ValueError, KeyError, TypeError):
            return {"kind": "contact", "text": "СРМ пока не подтвердила запись. Напишите «повторить», чтобы проверить отправку."}
        del self.contacts[session]
        return {"kind": "lead", "text": f"Заявка №{lead_id} сохранена в тестовой СРМ.", "lead_id": lead_id}
