# Contributing

This project should stay easy to run locally: Python standard library for the
CLI, SQLite for storage, and Poppler command-line tools for PDF extraction and
rendering.

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
taxhelper init --offline
python -m unittest discover -s tests -p 'test*.py'
```

Install Poppler first:

```bash
brew install poppler
```

or on Debian/Ubuntu:

```bash
sudo apt-get install poppler-utils
```

## Data Changes

- Prefer official sources from `skat.dk`, `info.skat.dk`, and
  `retsinformation.dk`.
- Keep source URLs and tax years explicit.
- Do not commit generated `tax_rules.sqlite` databases or filled PDF outputs.
- If rubrik parsing changes, add focused tests around the parser behavior.
