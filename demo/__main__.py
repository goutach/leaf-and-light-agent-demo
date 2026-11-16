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
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .agent import Agent, NAME, configured_router


WEB = Path(__file__).with_name("web")
MAX_BODY = 12_288


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
                created_at TEXT NOT NULL
            )""")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def create(self, request_id, name, phone):
        with closing(self.connect()) as db, db:
            db.execute("""INSERT INTO leads (request_id,name,phone,created_at) VALUES (?,?,?,?)
                ON CONFLICT(request_id) DO NOTHING""", (request_id, name, phone, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            return dict(db.execute("SELECT id,name,phone,created_at FROM leads WHERE request_id=?", (request_id,)).fetchone())

    def list(self):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute("SELECT id,name,phone,created_at FROM leads ORDER BY id DESC LIMIT 30")]


class CRMClient:
    def __init__(self, port, token):
        self.url = f"http://127.0.0.1:{port}/api/leads"
        self.token = token

    def request(self, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        request = Request(self.url, headers=headers, data=data)
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def create(self, request_id, name, phone):
        return self.request({"request_id": request_id, "name": name, "phone": phone})

    def list(self):
        return self.request()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    @property
    def path_only(self):
        return urlsplit(self.path).path

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

    def payload(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= MAX_BODY:
            raise ValueError("Некорректный размер запроса")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Ожидается JSON")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("Ожидается JSON-объект")
        return value


class CRMHandler(Handler):
    def authorize(self):
        if self.path_only != "/api/leads":
            self.send_json(404, {"error": "Маршрут не найден"})
            return False
        token = self.headers.get("Authorization", "")
        if not secrets.compare_digest(token, "Bearer " + self.server.token):
            self.send_json(401, {"error": "Требуется ключ тестовой СРМ"})
            return False
        return True

    def do_GET(self):
        if self.authorize():
            self.send_json(200, {"leads": self.server.store.list()})

    def do_POST(self):
        if not self.authorize():
            return
        try:
            payload = self.payload()
            request_id, name, phone = (payload.get(key) for key in ("request_id", "name", "phone"))
            if not all(isinstance(value, str) for value in (request_id, name, phone)):
                raise ValueError("Нужны ключ запроса, имя и телефон")
            if not re.fullmatch(r"[a-f0-9]{64}", request_id) or not NAME.fullmatch(name) or not re.fullmatch(r"\+7\d{10}", phone):
                raise ValueError("Проверьте имя и телефон")
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return
        self.send_json(200, self.server.store.create(request_id, name, phone))


class DemoHandler(Handler):
    def do_GET(self):
        if self.path_only == "/api/leads":
            try:
                result = self.server.agent.crm.list()
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
        if self.path_only != "/api/chat":
            self.send_json(404, {"error": "Маршрут не найден"})
            return
        try:
            data = self.payload()
            session, text = data.get("session"), data.get("message")
            if not isinstance(session, str) or not re.fullmatch(r"[a-zA-Z0-9-]{16,80}", session):
                raise ValueError("Некорректный диалог")
            if not isinstance(text, str) or not 0 < len(text.strip()) <= 1000:
                raise ValueError("Напишите сообщение до 1 000 символов")
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return
        result = self.server.agent.chat(session, text)
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
    args = parser.parse_args()
    app, crm = build_servers(args.port, args.crm_port, args.db, configured_router())
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
