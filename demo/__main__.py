import argparse
import json
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from .agent import Agent, HANDOFF_CONFIRMED, NAME, configured_router
from .catalogue import check_snapshot


WEB = Path(__file__).with_name("web")
MAX_BODY = 12_288
SESSION = re.compile(r"[a-zA-Z0-9-]{16,80}")


def valid_text(value, limit=1000):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit


def valid_session(value):
    return isinstance(value, str) and SESSION.fullmatch(value)


class LeadStore:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                request TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(leads)")}
            if "request" not in columns:
                db.execute("ALTER TABLE leads ADD COLUMN request TEXT NOT NULL DEFAULT ''")
            if "session" not in columns:
                db.execute("ALTER TABLE leads ADD COLUMN session TEXT")
            if "quote" not in columns:
                db.execute("ALTER TABLE leads ADD COLUMN quote TEXT")
            db.executescript("""
                CREATE UNIQUE INDEX IF NOT EXISTS leads_session ON leads(session);
                CREATE TABLE IF NOT EXISTS lead_messages (
                    id INTEGER PRIMARY KEY,
                    lead_id INTEGER NOT NULL REFERENCES leads(id),
                    role TEXT NOT NULL CHECK(role IN ('user', 'agent', 'manager')),
                    text TEXT NOT NULL,
                    event TEXT,
                    UNIQUE(lead_id, event)
                );
                CREATE INDEX IF NOT EXISTS lead_messages_by_lead ON lead_messages(lead_id, id);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def create(self, request_id, name, phone, request, session, history, event, quote):
        stored_quote = json.dumps(quote, ensure_ascii=False) if quote is not None else None
        with closing(self.connect()) as db, db:
            created = db.execute("""INSERT INTO leads (request_id,name,phone,request,created_at,session,quote)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                (request_id, name, phone, request, datetime.now(timezone.utc).isoformat(timespec="seconds"), session, stored_quote))
            lead = db.execute("SELECT id FROM leads WHERE session=?", (session,)).fetchone()
            if created.rowcount:
                db.executemany("INSERT INTO lead_messages (lead_id,role,text,event) VALUES (?,?,?,?)",
                               ((lead["id"], row["role"], row["text"], event if index == len(history) - 1 else None)
                                for index, row in enumerate(history)))
                db.execute("INSERT INTO lead_messages (lead_id,role,text) VALUES (?, 'agent', ?)",
                           (lead["id"], HANDOFF_CONFIRMED))
            return dict(lead)

    def list(self):
        with closing(self.connect()) as db:
            rows = db.execute("""SELECT id,name,phone,request,created_at,quote,
                session IS NOT NULL AS has_dialog FROM leads ORDER BY id DESC LIMIT 30""")
            return [{**row, "quote": json.loads(row["quote"]) if row["quote"] else None} for row in rows]

    def dialog(self, session, lead_id):
        with closing(self.connect()) as db:
            lead = db.execute("SELECT id FROM leads WHERE session IS NOT NULL AND (session=? OR id=?)",
                              (session, lead_id)).fetchone()
            if lead is None:
                return {"lead_id": None, "messages": []}
            messages = db.execute("SELECT role,text FROM lead_messages WHERE lead_id=? ORDER BY id", (lead["id"],))
            return {"lead_id": lead["id"], "messages": [dict(row) for row in messages]}

    def append(self, lead_id, role, text, event):
        with closing(self.connect()) as db, db:
            result = db.execute("""INSERT INTO lead_messages(lead_id,role,text,event)
                SELECT id,?,?,? FROM leads WHERE id=? AND session IS NOT NULL
                ON CONFLICT(lead_id,event) DO NOTHING""", (role, text, event, lead_id))
            if result.rowcount:
                return True
            saved = db.execute("SELECT role,text FROM lead_messages WHERE lead_id=? AND event=?",
                               (lead_id, event)).fetchone()
            return saved is not None and tuple(saved) == (role, text)


class CRMClient:
    def __init__(self, port, token):
        self.url = f"http://127.0.0.1:{port}/api/"
        self.token = token

    def request(self, path, payload=None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        request = Request(self.url + path, headers=headers, data=data)
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def create(self, request_id, name, phone, request, session, history, event, quote):
        return self.request("leads", {"request_id": request_id, "name": name, "phone": phone, "request": request,
                                      "session": session, "history": history, "event": event, "quote": quote})

    def list(self):
        return self.request("leads")

    def dialog(self, *, session=None, lead_id=None):
        query = {"session": session} if session is not None else {"lead_id": lead_id}
        return self.request("dialog?" + urlencode(query))

    def append(self, lead_id, role, text, event):
        return self.request("dialog", {"lead_id": lead_id, "role": role, "text": text, "event": event})["saved"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    @property
    def path_only(self):
        return urlsplit(self.path).path

    def dialog_query(self):
        query = parse_qs(urlsplit(self.path).query)
        session = query.get("session", [None])[0]
        raw_id = query.get("lead_id", [""])[0]
        return (session if valid_session(session) else None,
                int(raw_id) if re.fullmatch(r"[1-9]\d{0,9}", raw_id) else None)

    def respond(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status, data):
        self.respond(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def payload(self, max_size=MAX_BODY):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= max_size:
            raise ValueError("Некорректный размер запроса")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Ожидается JSON")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("Ожидается JSON-объект")
        return value


class CRMHandler(Handler):
    def authorize(self):
        if self.path_only not in ("/api/leads", "/api/dialog"):
            self.send_json(404, {"error": "Маршрут не найден"})
            return False
        token = self.headers.get("Authorization", "")
        if not secrets.compare_digest(token, "Bearer " + self.server.token):
            self.send_json(401, {"error": "Требуется ключ тестовой СРМ"})
            return False
        return True

    def do_GET(self):
        if not self.authorize():
            return
        if self.path_only == "/api/leads":
            self.send_json(200, {"leads": self.server.store.list()})
            return
        session, lead_id = self.dialog_query()
        if not session and not lead_id:
            self.send_json(400, {"error": "Укажите диалог или номер заявки"})
            return
        self.send_json(200, self.server.store.dialog(session, lead_id))

    def do_POST(self):
        if not self.authorize():
            return
        if self.path_only == "/api/dialog":
            try:
                item = self.payload()
                lead, role, text, event = (item.get(key) for key in ("lead_id", "role", "text", "event"))
                if type(lead) is not int or role not in ("user", "manager") or not valid_text(text) or not valid_session(event):
                    raise ValueError("Проверьте заявку, участника и текст")
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            else:
                self.send_json(200, {"saved": self.server.store.append(lead, role, text.strip(), event)})
            return
        try:
            payload = self.payload(128 * 1024)
            request_id, name, phone = (payload.get(key) for key in ("request_id", "name", "phone"))
            request = payload.get("request", "")
            if not all(isinstance(value, str) for value in (request_id, name, phone)):
                raise ValueError("Нужны ключ запроса, имя и телефон")
            if not re.fullmatch(r"[a-f0-9]{64}", request_id) or not NAME.fullmatch(name) or not re.fullmatch(r"\+7\d{10}", phone):
                raise ValueError("Проверьте имя и телефон")
            if not isinstance(request, str) or len(request) > 300:
                raise ValueError("Комментарий должен быть короче 300 символов")
            session, history, event = (payload.get(key) for key in ("session", "history", "event"))
            quote = payload.get("quote")
            if quote is not None:
                quote = check_snapshot(quote)
            if not valid_session(session) or not valid_session(event):
                raise ValueError("Некорректный диалог")
            if (not isinstance(history, list) or not 1 <= len(history) <= 20
                    or any(not isinstance(item, dict) or item.get("role") not in ("user", "agent")
                           or not valid_text(item.get("text"), 1500) for item in history)):
                raise ValueError("Некорректная история диалога")
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return
        self.send_json(200, self.server.store.create(request_id, name, phone, request, session, history, event, quote))


class DemoHandler(Handler):
    def do_GET(self):
        if self.path_only in ("/api/leads", "/api/dialog"):
            try:
                crm = self.server.agent.crm
                if self.path_only == "/api/leads":
                    result = crm.list()
                else:
                    session, lead_id = self.dialog_query()
                    result = crm.dialog(session=session, lead_id=lead_id)
            except (URLError, TimeoutError, ValueError):
                self.send_json(503, {"error": "Тестовая СРМ временно недоступна"})
            else:
                self.send_json(200, result)
            return
        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
        }
        item = files.get(self.path_only)
        if not item:
            self.send_json(404, {"error": "Маршрут не найден"})
            return
        name, content_type = item
        self.respond(200, (WEB / name).read_bytes(), content_type)

    def do_POST(self):
        if self.path_only == "/api/reply":
            try:
                payload = self.payload()
                lead, text, event = (payload.get(key) for key in ("lead_id", "message", "event"))
                if type(lead) is not int or not valid_text(text) or not valid_session(event):
                    raise ValueError("Укажите заявку и сообщение до 1 000 символов")
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            try:
                saved = self.server.agent.crm.append(lead, "manager", text, event)
            except (URLError, TimeoutError, ValueError):
                self.send_json(503, {"error": "СРМ пока не подтвердила запись ответа"})
            else:
                self.send_json(200 if saved else 409, {"saved": saved})
            return
        if self.path_only != "/api/chat":
            self.send_json(404, {"error": "Маршрут не найден"})
            return
        try:
            data = self.payload()
            session, text = data.get("session"), data.get("message")
            if not valid_session(session):
                raise ValueError("Некорректный диалог")
            if not valid_text(text):
                raise ValueError("Напишите сообщение до 1 000 символов")
            event = data.get("event")
            if event is not None and not valid_session(event):
                raise ValueError("Некорректный номер сообщения")
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return
        try:
            result = self.server.agent.chat(session, text, event)
        except (URLError, TimeoutError, ValueError):
            self.send_json(503, {"error": "СРМ пока не подтвердила запись сообщения"})
            return
        self.send_json(200, result)


def build_servers(port, crm_port, db_path, router=None):
    store = LeadStore(db_path)
    crm = ThreadingHTTPServer(("127.0.0.1", crm_port), CRMHandler)
    crm.store = store
    crm.token = secrets.token_urlsafe(32)
    try:
        app = ThreadingHTTPServer(("127.0.0.1", port), DemoHandler)
    except OSError:
        crm.server_close()
        raise
    app.agent = Agent(CRMClient(crm.server_port, crm.token), router)
    return app, crm


def main():
    parser = argparse.ArgumentParser(description="Демо агента Лист и свет")
    parser.add_argument("--port", type=int, default=0, help="Порт интерфейса, 0 выбирает свободный")
    parser.add_argument("--crm-port", type=int, default=0)
    parser.add_argument("--db", type=Path, default=Path("data/leads.sqlite3"))
    parser.add_argument("--semantic", action="store_true", help="Локальная нейросеть для поиска по справочнику")
    args = parser.parse_args()
    if args.semantic:
        from .semantic import SemanticRouter
        router = SemanticRouter()
    else:
        router = configured_router()
    app, crm = build_servers(args.port, args.crm_port, args.db, router)
    worker = Thread(target=crm.serve_forever, name="demo-crm")
    worker.start()
    print(f"UI: http://127.0.0.1:{app.server_port}", flush=True)
    print(f"CRM: http://127.0.0.1:{crm.server_port}", flush=True)
    try:
        app.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.server_close()
        crm.shutdown()
        crm.server_close()
        worker.join()


if __name__ == "__main__":
    main()
