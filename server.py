#!/usr/bin/env python3
import json
import os
import sqlite3
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "progress.db"
HOST = os.environ.get("GERMAN_DECK_HOST", "0.0.0.0")
PORT = int(os.environ.get("GERMAN_DECK_PORT", "4173"))


def connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with connect() as connection:
        connection.execute(
            """
            create table if not exists card_progress (
              card_id text primary key,
              shown_count integer not null default 0,
              true_count integer not null default 0,
              false_count integer not null default 0,
              last_shown_at integer,
              last_correct_at integer
            )
            """
        )


def row_to_stats(row):
    return {
        "shownCount": row["shown_count"],
        "trueCount": row["true_count"],
        "falseCount": row["false_count"],
        "lastShownAt": row["last_shown_at"],
        "lastCorrectAt": row["last_correct_at"],
    }


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    if length == 0:
        return {}
    return json.loads(handler.rfile.read(length))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def send_json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if urlparse(self.path).path == "/api/progress":
            with connect() as connection:
                rows = connection.execute("select * from card_progress").fetchall()
            self.send_json({"progress": {row["card_id"]: row_to_stats(row) for row in rows}})
            return

        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = read_json(self)
            if path == "/api/grade":
                self.record_grade(payload)
            elif path == "/api/import-progress":
                self.import_progress(payload)
            elif path == "/api/reset":
                self.reset_progress()
            else:
                self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def record_grade(self, payload):
        card_id = str(payload["cardId"])
        now = int(payload["now"])
        was_correct = bool(payload["wasCorrect"])

        with connect() as connection:
            connection.execute(
                """
                insert into card_progress (card_id)
                values (?)
                on conflict(card_id) do nothing
                """,
                (card_id,),
            )
            connection.execute(
                """
                update card_progress
                set shown_count = shown_count + 1,
                    last_shown_at = ?
                where card_id = ?
                """,
                (now, card_id),
            )

            if was_correct:
                connection.execute(
                    """
                    update card_progress
                    set true_count = true_count + 1,
                        last_correct_at = ?
                    where card_id = ?
                    """,
                    (now, card_id),
                )
            else:
                connection.execute(
                    """
                    update card_progress
                    set false_count = false_count + 1
                    where card_id = ?
                    """,
                    (card_id,),
                )

            row = connection.execute(
                "select * from card_progress where card_id = ?",
                (card_id,),
            ).fetchone()
        self.send_json({"stats": row_to_stats(row)})

    def import_progress(self, payload):
        progress = payload.get("progress", {})
        with connect() as connection:
            for card_id, stats in progress.items():
                connection.execute(
                    """
                    insert into card_progress (
                      card_id, shown_count, true_count, false_count, last_shown_at, last_correct_at
                    )
                    values (?, ?, ?, ?, ?, ?)
                    on conflict(card_id) do update set
                      shown_count = max(shown_count, excluded.shown_count),
                      true_count = max(true_count, excluded.true_count),
                      false_count = max(false_count, excluded.false_count),
                      last_shown_at = max(coalesce(last_shown_at, 0), coalesce(excluded.last_shown_at, 0)),
                      last_correct_at = max(coalesce(last_correct_at, 0), coalesce(excluded.last_correct_at, 0))
                    """,
                    (
                        str(card_id),
                        int(stats.get("shownCount") or 0),
                        int(stats.get("trueCount") or 0),
                        int(stats.get("falseCount") or 0),
                        stats.get("lastShownAt"),
                        stats.get("lastCorrectAt"),
                    ),
                )
        self.send_json({"ok": True})

    def reset_progress(self):
        with connect() as connection:
            connection.execute("delete from card_progress")
        self.send_json({"ok": True})


def main():
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Serving German Deck Trainer at http://{HOST}:{PORT}")
    print("On another device, open http://YOUR_MAC_IP:4173")
    print(f"Saving progress in {DB_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
