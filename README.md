# German Deck Trainer

I built this personal German word memorizer for myself. It runs locally and keeps my study progress on the computer.

The trainer shows a word, reveals the answer, and records whether it was remembered correctly. New words and words answered incorrectly receive more attention. It can be used in the browser or through the optional Telegram bot that I connect from my phone.

## Run

```sh
pip install -r requirements.txt
python scripts/build_vocab_cards.py
python server.py
```

Then open <http://localhost:4173>.

Progress is saved locally in `progress.db`. Vocabulary PDFs, generated card JSON, Anki packages, environment settings, and the progress database are not committed to Git.
