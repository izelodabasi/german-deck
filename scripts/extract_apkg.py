#!/usr/bin/env python3
import argparse
import html
import json
import re
import sqlite3
import tempfile
import zipfile
from pathlib import Path


FIELD_SEPARATOR = "\x1f"


def strip_sound_markers(value):
    return re.sub(r"\[sound:[^\]]+\]", "", value).strip()


def clean_front(value):
    value = strip_sound_markers(value)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return "\n".join(lines)


def clean_back(value):
    value = strip_sound_markers(value)
    value = re.sub(r"<br\s*/?>", "<br>", value, flags=re.IGNORECASE)
    return value.strip()


def extract_collection(apkg_path):
    with zipfile.ZipFile(apkg_path) as archive:
        for candidate in ("collection.anki21b", "collection.anki21", "collection.anki2"):
            if candidate in archive.namelist():
                with tempfile.NamedTemporaryFile(delete=False) as tmp:
                    tmp.write(archive.read(candidate))
                    return Path(tmp.name)
    raise RuntimeError("No Anki collection database found in package.")


def read_cards(db_path):
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            """
            select notes.id, notes.flds
            from notes
            join cards on cards.nid = notes.id
            where cards.ord = 0
            order by notes.id
            """
        ).fetchall()
    finally:
        connection.close()

    cards = []
    for note_id, fields in rows:
        parts = fields.split(FIELD_SEPARATOR)
        if len(parts) < 2:
            continue
        front = clean_front(parts[0])
        back = clean_back(parts[1])
        if front and back:
            cards.append({"id": str(note_id), "front": front, "back": back})
    return cards


def main():
    parser = argparse.ArgumentParser(description="Extract Basic front/back cards from an Anki .apkg file.")
    parser.add_argument("apkg", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    db_path = extract_collection(args.apkg)
    try:
        cards = read_cards(db_path)
    finally:
        db_path.unlink(missing_ok=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"cards": cards}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(cards)} cards to {args.output}")


if __name__ == "__main__":
    main()
