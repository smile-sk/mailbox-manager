# Mailbox Manager

Desktop app for temporary mailboxes: register, read and switch between mailboxes from a
single window, with per-provider specifics handled.

## Layout

- `app.py` - core logic
- `gui.py`, `run_gui.py` - PyQt interface
- `core/` - providers, database, paths
- `locales/` - translations
- `scripts/`, `tools/` - helpers

## Run

```bash
pip install -r requirements.txt
python run_gui.py
```

Runtime data and credentials live in `data/` and `logs/` (gitignored).
