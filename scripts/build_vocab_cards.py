#!/usr/bin/env python3
import argparse
import hashlib
import html
import json
import re
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError as error:
    raise SystemExit(
        "Missing dependency: pypdf. Install it with `.venv/bin/python -m pip install pypdf`."
    ) from error


SECTION_NAMES = {
    "Nouns",
    "Verbs",
    "Adjectives",
    "Adverbs",
    "Useful Expressions",
    "Common Expressions",
}

SECTION_LABELS = {
    "Nouns": "Noun",
    "Verbs": "Verb",
    "Adjectives": "Adjective",
    "Adverbs": "Adverb",
    "Useful Expressions": "Useful expression",
    "Common Expressions": "Common expression",
}

PAIR_SEPARATOR = re.compile(r"\s+(?:—|–|--)\s+")
PAGE_NUMBER = re.compile(r"^\d+$")


def pdf_text(path):
    reader = PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def topic_from_text(text, fallback):
    match = re.search(r"^Topic:\s*(.+)$", text, flags=re.MULTILINE)
    if match:
        return match.group(1).strip()
    return re.sub(r"^\d+-", "", fallback.stem).replace(" Vocabulary", "").strip()


def parse_entries(path):
    text = pdf_text(path)
    topic = topic_from_text(text, path)
    section = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line in {"Vocabulary", "•"} or PAGE_NUMBER.match(line):
            continue

        if line in SECTION_NAMES:
            section = line
            continue

        if line.startswith("Topic:"):
            continue

        parts = PAIR_SEPARATOR.split(line, maxsplit=1)
        if len(parts) != 2 or section is None:
            continue

        german, english = (clean_value(part) for part in parts)
        if german and english:
            yield {
                "topic": topic,
                "section": section,
                "german": german,
                "english": english,
            }


def card_id(entry):
    value = "\x1f".join(
        [entry["topic"], entry["section"], entry["german"], entry["english"]]
    )
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def clean_value(value):
    value = re.sub(r"\s+", " ", value.strip())
    return re.sub(r"\s+([,.;:])", r"\1", value)


def card_from_entry(entry):
    german = html.escape(entry["german"])
    topic = html.escape(entry["topic"])
    section = html.escape(SECTION_LABELS.get(entry["section"], entry["section"]))

    return {
        "id": card_id(entry),
        "front": entry["english"],
        "back": (
            f"{german}<br><br>"
            f"<b>Topic:</b> {topic}<br>"
            f"<b>Type:</b> {section}"
        ),
    }


def natural_vocab_order(path):
    match = re.match(r"^(\d+)-", path.name)
    return (int(match.group(1)) if match else 999, path.name)


def build_cards(vocab_dir):
    cards = []
    seen_ids = set()
    for path in sorted(vocab_dir.glob("*.pdf"), key=natural_vocab_order):
        for entry in parse_entries(path):
            card = card_from_entry(entry)
            if card["id"] in seen_ids:
                continue
            seen_ids.add(card["id"])
            cards.append(card)
    return cards


def main():
    parser = argparse.ArgumentParser(description="Build flashcards from vocabulary PDFs.")
    parser.add_argument("--vocab-dir", type=Path, default=Path("vocab"))
    parser.add_argument("--output", type=Path, default=Path("data/cards.json"))
    args = parser.parse_args()

    cards = build_cards(args.vocab_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"cards": cards}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(cards)} cards to {args.output}")


if __name__ == "__main__":
    main()
