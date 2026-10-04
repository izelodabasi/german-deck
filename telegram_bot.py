#!/usr/bin/env python3
import html
import json
import math
import os
import random
import re
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CARDS_PATH = ROOT / "data" / "cards.json"
DB_PATH = ROOT / "progress.db"
API_TIMEOUT = 45
MAX_CONSECUTIVE_FAILURES = 5


def load_env():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_env()
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
AUTH_KEY = os.environ.get("TELEGRAM_BOT_AUTH_KEY")
DAILY_GOAL = int(os.environ.get("TELEGRAM_DAILY_WORD_GOAL", "30"))
REMINDER_HOUR = int(os.environ.get("TELEGRAM_REMINDER_HOUR", "22"))
REMINDER_MINUTE = int(os.environ.get("TELEGRAM_REMINDER_MINUTE", "0"))
ALLOWED_CHAT_IDS = {
    int(value.strip())
    for value in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
    if value.strip()
}


def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def validate_config():
    if not TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env before running this bot.")
    try:
        TOKEN.encode("ascii")
    except UnicodeEncodeError as error:
        raise RuntimeError("TELEGRAM_BOT_TOKEN must contain only ASCII characters.") from error


def connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with connect() as connection:
        connection.execute(
            """
            create table if not exists telegram_progress (
              chat_id integer not null,
              card_id text not null,
              shown_count integer not null default 0,
              true_count integer not null default 0,
              false_count integer not null default 0,
              last_shown_at integer,
              last_correct_at integer,
              primary key (chat_id, card_id)
            )
            """
        )
        connection.execute(
            """
            create table if not exists telegram_sessions (
              chat_id integer primary key,
              current_card_id text,
              answer_shown integer not null default 0
            )
            """
        )
        connection.execute(
            """
            create table if not exists telegram_authorized_chats (
              chat_id integer primary key,
              authorized_at integer not null
            )
            """
        )
        connection.execute(
            """
            create table if not exists telegram_daily_counts (
              chat_id integer not null,
              day text not null,
              reviewed_count integer not null default 0,
              primary key (chat_id, day)
            )
            """
        )
        connection.execute(
            """
            create table if not exists telegram_daily_reminders (
              chat_id integer not null,
              day text not null,
              sent_at integer not null,
              primary key (chat_id, day)
            )
            """
        )
        connection.execute(
            """
            create table if not exists telegram_known_cards (
              chat_id integer not null,
              card_id text not null,
              known_at integer not null,
              primary key (chat_id, card_id)
            )
            """
        )


def load_cards():
    cards = json.loads(CARDS_PATH.read_text(encoding="utf-8"))["cards"]
    return {card["id"]: card for card in cards}, cards


def request(method, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=data,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram {method} failed: HTTP {error.code} {body}") from error
    except (socket.timeout, TimeoutError, urllib.error.URLError) as error:
        raise RuntimeError(f"Telegram {method} timed out or could not connect: {error}") from error


def html_to_text(value):
    value = re.sub(r"\[sound:[^\]]+\]", "", value)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def front_text(card):
    return card["front"].replace("\n", "\n\n")


def stats_for(connection, chat_id, card_id):
    row = connection.execute(
        """
        select * from telegram_progress
        where chat_id = ? and card_id = ?
        """,
        (chat_id, card_id),
    ).fetchone()

    if row:
        return row

    connection.execute(
        "insert into telegram_progress (chat_id, card_id) values (?, ?)",
        (chat_id, card_id),
    )
    return connection.execute(
        """
        select * from telegram_progress
        where chat_id = ? and card_id = ?
        """,
        (chat_id, card_id),
    ).fetchone()


def card_weight(row):
    now = int(time.time() * 1000)
    last_correct_at = row["last_correct_at"] or 0
    days_since_correct = max(1, (now - last_correct_at) / 86400000)
    mistake_boost = 1 + row["false_count"] * 0.35
    new_card_boost = 12 if row["shown_count"] == 0 else 1
    return math.log2(days_since_correct + 1) * mistake_boost + new_card_boost


def choose_card(chat_id, cards):
    with connect() as connection:
        current = connection.execute(
            "select current_card_id from telegram_sessions where chat_id = ?",
            (chat_id,),
        ).fetchone()
        current_id = current["current_card_id"] if current else None
        known_ids = {
            row["card_id"]
            for row in connection.execute(
                "select card_id from telegram_known_cards where chat_id = ?",
                (chat_id,),
            ).fetchall()
        }

        available = [card for card in cards if card["id"] not in known_ids]
        candidates = [card for card in available if card["id"] != current_id] or available
        if not candidates:
            return None

        rows = [stats_for(connection, chat_id, card["id"]) for card in candidates]
        weights = [card_weight(row) for row in rows]

    marker = random.random() * sum(weights)
    for card, weight in zip(candidates, weights):
        marker -= weight
        if marker <= 0:
            return card
    return candidates[-1]


def set_session(chat_id, card_id, answer_shown=False):
    with connect() as connection:
        connection.execute(
            """
            insert into telegram_sessions (chat_id, current_card_id, answer_shown)
            values (?, ?, ?)
            on conflict(chat_id) do update set
              current_card_id = excluded.current_card_id,
              answer_shown = excluded.answer_shown
            """,
            (chat_id, card_id, int(answer_shown)),
        )


def current_card_id(chat_id):
    with connect() as connection:
        row = connection.execute(
            "select current_card_id from telegram_sessions where chat_id = ?",
            (chat_id,),
        ).fetchone()
    return row["current_card_id"] if row else None


def record_grade(chat_id, card_id, was_correct):
    now = int(time.time() * 1000)
    with connect() as connection:
        stats_for(connection, chat_id, card_id)
        connection.execute(
            """
            update telegram_progress
            set shown_count = shown_count + 1,
                last_shown_at = ?
            where chat_id = ? and card_id = ?
            """,
            (now, chat_id, card_id),
        )
        if was_correct:
            connection.execute(
                """
                update telegram_progress
                set true_count = true_count + 1,
                    last_correct_at = ?
                where chat_id = ? and card_id = ?
                """,
                (now, chat_id, card_id),
            )
        else:
            connection.execute(
                """
                update telegram_progress
                set false_count = false_count + 1
                where chat_id = ? and card_id = ?
                """,
                (chat_id, card_id),
            )
        increment_daily_count(connection, chat_id)


def mark_known(chat_id, card_id):
    with connect() as connection:
        connection.execute(
            """
            insert into telegram_known_cards (chat_id, card_id, known_at)
            values (?, ?, ?)
            on conflict(chat_id, card_id) do update set known_at = excluded.known_at
            """,
            (chat_id, card_id, int(time.time() * 1000)),
        )


def today_key():
    return datetime.now().strftime("%Y-%m-%d")


def increment_daily_count(connection, chat_id):
    connection.execute(
        """
        insert into telegram_daily_counts (chat_id, day, reviewed_count)
        values (?, ?, 1)
        on conflict(chat_id, day) do update set
          reviewed_count = reviewed_count + 1
        """,
        (chat_id, today_key()),
    )


def daily_count(chat_id, day=None):
    with connect() as connection:
        row = connection.execute(
            """
            select reviewed_count from telegram_daily_counts
            where chat_id = ? and day = ?
            """,
            (chat_id, day or today_key()),
        ).fetchone()
    return row["reviewed_count"] if row else 0


def send_message(chat_id, text, buttons=None):
    if len(text) > 3900:
        chunks = [text[index:index + 3900] for index in range(0, len(text), 3900)]
        for chunk in chunks[:-1]:
            send_message(chat_id, chunk)
        send_message(chat_id, chunks[-1], buttons)
        return

    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    request("sendMessage", payload)


def is_authorized(chat_id):
    if ALLOWED_CHAT_IDS:
        return is_allowed_chat(chat_id)

    if not AUTH_KEY:
        return False

    with connect() as connection:
        row = connection.execute(
            "select 1 from telegram_authorized_chats where chat_id = ?",
            (chat_id,),
        ).fetchone()
    return row is not None


def is_allowed_chat(chat_id):
    return not ALLOWED_CHAT_IDS or chat_id in ALLOWED_CHAT_IDS


def authorize_chat(chat_id):
    with connect() as connection:
        connection.execute(
            """
            insert into telegram_authorized_chats (chat_id, authorized_at)
            values (?, ?)
            on conflict(chat_id) do update set authorized_at = excluded.authorized_at
            """,
            (chat_id, int(time.time() * 1000)),
        )


def forget_chat(chat_id):
    with connect() as connection:
        connection.execute("delete from telegram_authorized_chats where chat_id = ?", (chat_id,))
        connection.execute("delete from telegram_sessions where chat_id = ?", (chat_id,))


def require_auth(chat_id):
    if not is_allowed_chat(chat_id):
        log(f"chat={chat_id} blocked_not_allowed")
        send_message(chat_id, "This bot is private.")
        return False

    if is_authorized(chat_id):
        return True

    if ALLOWED_CHAT_IDS:
        return True

    if AUTH_KEY:
        send_message(chat_id, "Send the access key to unlock this deck.")
    else:
        send_message(
            chat_id,
            "Bot auth is not configured. Add TELEGRAM_BOT_AUTH_KEY to .env on the Mac, then restart the bot.",
        )
    return False


def answer_callback(callback_id):
    try:
        request("answerCallbackQuery", {"callback_query_id": callback_id})
    except RuntimeError as error:
        log(f"answer_callback ignored: {error}")


def send_next_card(chat_id, cards):
    card = choose_card(chat_id, cards)
    if not card:
        send_message(chat_id, "All cards are marked as known. Use /reset to start over.")
        return

    log(f"chat={chat_id} next_card card_id={card['id']}")
    set_session(chat_id, card["id"], False)
    send_message(
        chat_id,
        front_text(card),
        [[{"text": "Show answer", "callback_data": "show"}]],
    )


def show_answer(chat_id, cards_by_id):
    card_id = current_card_id(chat_id)
    log(f"chat={chat_id} show_answer card_id={card_id}")
    if not card_id or card_id not in cards_by_id:
        send_message(chat_id, "Send /card to start.")
        return

    set_session(chat_id, card_id, True)
    send_message(
        chat_id,
        html_to_text(cards_by_id[card_id]["back"]),
        [[
            {"text": "Need practice", "callback_data": "grade:false"},
            {"text": "Got it", "callback_data": "grade:true"},
            {"text": "I know this word", "callback_data": "grade:known"},
        ]],
    )


def send_stats(chat_id):
    today_count = daily_count(chat_id)
    with connect() as connection:
        row = connection.execute(
            """
            select
              count(*) filter (where shown_count > 0) as seen,
              coalesce(sum(true_count), 0) as true_count,
              coalesce(sum(false_count), 0) as false_count
            from telegram_progress
            where chat_id = ?
            """,
            (chat_id,),
        ).fetchone()
        known_count = connection.execute(
            "select count(*) as count from telegram_known_cards where chat_id = ?",
            (chat_id,),
        ).fetchone()["count"]

    total_answers = row["true_count"] + row["false_count"]
    accuracy = round((row["true_count"] / total_answers) * 100) if total_answers else 0
    send_message(
        chat_id,
        (
            f"Today: {today_count}/{DAILY_GOAL}\n"
            f"Seen: {row['seen']}\n"
            f"Correct: {row['true_count']}\n"
            f"False: {row['false_count']}\n"
            f"Known: {known_count}\n"
            f"Accuracy: {accuracy}%"
        ),
    )


def reset_progress(chat_id):
    with connect() as connection:
        connection.execute("delete from telegram_progress where chat_id = ?", (chat_id,))
        connection.execute("delete from telegram_sessions where chat_id = ?", (chat_id,))
        connection.execute("delete from telegram_daily_counts where chat_id = ?", (chat_id,))
        connection.execute("delete from telegram_daily_reminders where chat_id = ?", (chat_id,))
        connection.execute("delete from telegram_known_cards where chat_id = ?", (chat_id,))
    send_message(chat_id, "Progress reset. Send /card to start again.")


def reset_all_telegram_data(chat_id):
    with connect() as connection:
        connection.execute("delete from telegram_progress")
        connection.execute("delete from telegram_sessions")
        connection.execute("delete from telegram_authorized_chats")
        connection.execute("delete from telegram_daily_counts")
        connection.execute("delete from telegram_daily_reminders")
        connection.execute("delete from telegram_known_cards")
    log(f"chat={chat_id} reset_all_telegram_data")
    send_message(chat_id, "Telegram bot DB reset. Send /start and unlock again.")


def reminder_chat_ids():
    if ALLOWED_CHAT_IDS:
        return sorted(ALLOWED_CHAT_IDS)

    with connect() as connection:
        rows = connection.execute("select chat_id from telegram_authorized_chats").fetchall()
    return [row["chat_id"] for row in rows]


def reminder_already_sent(connection, chat_id, day):
    row = connection.execute(
        """
        select 1 from telegram_daily_reminders
        where chat_id = ? and day = ?
        """,
        (chat_id, day),
    ).fetchone()
    return row is not None


def mark_reminder_sent(connection, chat_id, day):
    connection.execute(
        """
        insert into telegram_daily_reminders (chat_id, day, sent_at)
        values (?, ?, ?)
        on conflict(chat_id, day) do nothing
        """,
        (chat_id, day, int(time.time() * 1000)),
    )


def maybe_send_daily_reminders():
    now = datetime.now()
    if (now.hour, now.minute) < (REMINDER_HOUR, REMINDER_MINUTE):
        return

    day = today_key()
    for chat_id in reminder_chat_ids():
        count = daily_count(chat_id, day)
        if count >= DAILY_GOAL:
            continue

        with connect() as connection:
            if reminder_already_sent(connection, chat_id, day):
                continue
            mark_reminder_sent(connection, chat_id, day)

        log(f"chat={chat_id} daily_reminder count={count} goal={DAILY_GOAL}")
        send_message(
            chat_id,
            f"Daily German check: you reviewed {count}/{DAILY_GOAL} words today. Send /card to finish the goal.",
        )


def handle_message(message, cards):
    chat_id = message["chat"]["id"]
    text = message.get("text", "")
    command = text.split(maxsplit=1)[0] if text.startswith("/") else "<text>"
    log(f"chat={chat_id} message={command}")

    if text.startswith("/start"):
        if require_auth(chat_id):
            send_message(
                chat_id,
                "German deck ready. Send /card for a card, /stats for progress, /reset to clear Telegram progress.",
            )
            send_next_card(chat_id, cards)
    elif text.startswith("/card"):
        if require_auth(chat_id):
            send_next_card(chat_id, cards)
    elif text.startswith("/stats"):
        if require_auth(chat_id):
            send_stats(chat_id)
    elif text.startswith("/today"):
        if require_auth(chat_id):
            send_message(chat_id, f"Today: {daily_count(chat_id)}/{DAILY_GOAL}")
    elif text.startswith("/resetdb"):
        if require_auth(chat_id):
            reset_all_telegram_data(chat_id)
    elif text.startswith("/reset"):
        if require_auth(chat_id):
            reset_progress(chat_id)
    elif text.startswith("/logout"):
        forget_chat(chat_id)
        send_message(chat_id, "Locked. Send /start to unlock again.")
    elif not ALLOWED_CHAT_IDS and AUTH_KEY and text.strip() == AUTH_KEY:
        authorize_chat(chat_id)
        send_message(chat_id, "Unlocked. German deck ready.")
        send_next_card(chat_id, cards)
    else:
        if require_auth(chat_id):
            send_message(chat_id, "Use /card, /stats, /reset, or /logout.")


def handle_callback(callback, cards_by_id, cards):
    chat_id = callback["message"]["chat"]["id"]
    data = callback.get("data", "")
    log(f"chat={chat_id} click={data}")
    answer_callback(callback["id"])

    if not require_auth(chat_id):
        return

    if data == "show":
        show_answer(chat_id, cards_by_id)
    elif data.startswith("grade:"):
        card_id = current_card_id(chat_id)
        log(f"chat={chat_id} grade={data} card_id={card_id}")
        if not card_id:
            send_message(chat_id, "Send /card to start.")
            return
        record_grade(chat_id, card_id, data in {"grade:true", "grade:known"})
        if data == "grade:known":
            mark_known(chat_id, card_id)
        send_next_card(chat_id, cards)


def poll():
    validate_config()
    init_db()
    cards_by_id, cards = load_cards()
    offset = None
    consecutive_failures = 0
    log("Telegram bot running. Press Ctrl+C to stop.")
    log(f"Saving Telegram progress in {DB_PATH}")

    while True:
        payload = {"timeout": 30}
        if offset is not None:
            payload["offset"] = offset

        try:
            updates = request("getUpdates", payload).get("result", [])
        except (RuntimeError, urllib.error.URLError) as error:
            consecutive_failures += 1
            log(f"Network error {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}: {error}. Retrying soon.")
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise RuntimeError(f"Stopping after {MAX_CONSECUTIVE_FAILURES} consecutive Telegram failures.")
            time.sleep(5)
            continue

        consecutive_failures = 0

        for update in updates:
            offset = update["update_id"] + 1
            try:
                if "message" in update:
                    handle_message(update["message"], cards)
                elif "callback_query" in update:
                    handle_callback(update["callback_query"], cards_by_id, cards)
            except Exception as error:
                log(f"update_id={update['update_id']} error={error}")
                chat_id = None
                if "message" in update:
                    chat_id = update["message"]["chat"]["id"]
                elif "callback_query" in update:
                    chat_id = update["callback_query"]["message"]["chat"]["id"]
                if chat_id:
                    send_message(chat_id, "Bot error on that action. Check the Mac terminal log.")

        try:
            maybe_send_daily_reminders()
        except Exception as error:
            log(f"daily_reminder error={error}")


if __name__ == "__main__":
    try:
        poll()
    except RuntimeError as error:
        print(f"Error: {error}")
