# Tax Helper

`taxhelper` is a small local CLI for understanding Danish annual tax assessment
notices (`årsopgørelse`). It stores structured rules, official source pages, and
search indexes in SQLite. It can also generate a review worksheet and fill a
copy of the official `04.003 Oplysningsskemaet` PDF from JSON values.

This is a research and lookup tool. It does not file, calculate, or optimize a
tax return, and it should not be treated as tax advice.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/rasmusrbj/taxhelper/main/install.sh | sh
taxhelper init
```

The raw curl installer uses `pipx`, installs Poppler when it can, and
automatically installs or refreshes the bundled `taxhelper` Agent Skill for
Codex and Claude Code. Poppler provides the `pdftotext`, `pdftohtml`, and
`pdftocairo` tools used for PDF scraping/filling. Restart those agent clients
after installation.

Or install directly with `pipx`:

```bash
pipx install git+https://github.com/rasmusrbj/taxhelper.git
taxhelper install-skills
taxhelper init
```

If you install manually, install Poppler first:

```bash
brew install poppler
```

or on Debian/Ubuntu:

```bash
sudo apt-get install poppler-utils
```

## Quick Start

```bash
taxhelper lookup "field 417"
taxhelper template --tag befordring
taxhelper fill-pdf data/example_fill_values.json --output filled-04003.pdf
taxhelper explain "kan jeg få kørselsfradrag?"
```

For local development:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
taxhelper init
```

`taxhelper init` creates/migrates `tax_rules.sqlite`, uses the bundled official
PDF, loads bundled seed rules, scrapes rubrikker from the PDF, fetches TastSelv
rubrik guidance, and rebuilds search/tag indexes. Re-running it is safe;
existing rubrik data is reused unless `--refresh` is passed.

By default, `taxhelper` uses the local project database at `tax_rules.sqlite`
when it exists. Override it only when needed with `TAX_HELPER_DB` or `--db`.

Common flags can be placed before or after the subcommand:

```bash
taxhelper --json lookup "rubrik 51"
taxhelper lookup "rubrik 51" --json
```

## CLI Commands

```bash
taxhelper init
taxhelper init --offline
taxhelper init --schema-only
taxhelper seed
taxhelper scrape-rubrics
taxhelper search "rejsefradrag rubrik 53"
taxhelper rubrics 37
taxhelper rubric 37
taxhelper tags
taxhelper tag bolig
taxhelper --json lookup "field 417"
taxhelper context "can I deduct transport to work?" --json --limit 3
taxhelper related 51 --json
taxhelper template --editable-only
taxhelper fill-pdf values.json --output filled-04003.pdf
taxhelper mcp
taxhelper install-skills
taxhelper stats
taxhelper show travel-deduction-2026
taxhelper amounts --year 2026
taxhelper fields rubrik
taxhelper sources "beskæftigelsesfradrag"
taxhelper import-url "https://skat.dk/..."
```

## Data Model

The database keeps two kinds of material:

- Structured rules: concise, searchable records with categories, years, known
  return fields, thresholds, rates, and source URLs.
- Rubrikker: scraped annual-statement/oplysningsskema rows with rubrik number,
  field number, section, lock/blanket notes, and attached TastSelv help text
  where `info.skat.dk` exposes a detail page.
- Source documents: official pages imported as searchable text, so the CLI can
  surface source material even before a rule has been normalized.
- Tags: deterministic taxonomy labels inferred from stable rubrik metadata.
  Tags are stored in SQLite and can be browsed or used as filters.

The generated SQLite database is local state and should not be committed. The
source PDF and small example/seed JSON files live in `data/` and are also bundled
as Python package data, so `pipx install ...` followed by `taxhelper init` works
without manual file copying.

## Bootstrap Modes

Use the default online bootstrap for full rubrik guidance:

```bash
taxhelper init
```

Use the bundled PDF only, without network guidance pages:

```bash
taxhelper init --offline
```

Re-fetch and re-scrape everything:

```bash
taxhelper init --refresh
```

Create only an empty schema:

```bash
taxhelper init --schema-only
```

## Scraping Rubrikker

The current scraper defaults to the official 2025/January 2026 form PDF and the
2025 TastSelv help-page version:

```bash
taxhelper scrape-rubrics
```

For the sources used during initial setup:

```bash
taxhelper scrape-rubrics \
  --pdf-url "https://skat.dk/media/ftiduwhm/04003_januar2026-t.pdf" \
  --guide-url "https://info.skat.dk/data.aspx?oid=2236973&chk=221074" \
  --guide-start-oid 2236970 \
  --guide-end-oid 2237100
```

This requires `pdftotext` from Poppler for PDF extraction.

The official PDF used for filling is stored locally at
`data/04003_januar2026-t.pdf`.

## Agent Usage

Agent/read commands open the SQLite database in read-only mode. Only explicit
maintenance commands mutate it: `init`, `seed`, `scrape-rubrics`, `import-url`,
`rebuild-fts`, and `rebuild-tags`.

Use `--json` for stable machine-readable output:

```bash
taxhelper lookup "field 417" --json
taxhelper context "can I deduct transport to work?" --json --limit 3 --max-chars 800
taxhelper search "rental income" --tag bolig --json
taxhelper tag befordring --json
taxhelper related 51 --json
taxhelper template --tag befordring --json
taxhelper stats --json
```

Good agent workflow:

- `lookup`: resolve exact rubrik/felt numbers and matching tags.
- `context`: retrieve compact source-linked context for a user question.
- `rubric`: fetch one full rubrik record.
- `tags` / `tag`: discover taxonomy filters.
- `related`: find rubrikker with overlapping tags.
- `template`: generate a review/fill-out worksheet from rubrikker.
- `stats`: inspect database coverage before answering.

## Agent Skill

The raw curl installer automatically installs or refreshes the bundled
`taxhelper` Agent Skill for local agents that support `SKILL.md` folders:

- Codex: `~/.codex/skills/taxhelper`
- Claude Code: `~/.claude/skills/taxhelper`

Install or refresh it manually:

```bash
taxhelper install-skills
taxhelper install-skills --force
taxhelper install-skills --target codex
taxhelper install-skills --path ~/.agents/skills
```

Restart your agent client after installing new skills.

## MCP Server

`taxhelper` includes a stdio MCP server for agents and desktop clients that
support the Model Context Protocol.

After installing and initializing:

```bash
taxhelper init
taxhelper-mcp
```

Typical MCP client config:

```json
{
  "mcpServers": {
    "taxhelper": {
      "command": "taxhelper-mcp",
      "env": {
        "TAX_HELPER_DB": "/absolute/path/to/tax_rules.sqlite"
      }
    }
  }
}
```

You can also run it through the main CLI:

```json
{
  "mcpServers": {
    "taxhelper": {
      "command": "taxhelper",
      "args": ["mcp", "--db", "/absolute/path/to/tax_rules.sqlite"]
    }
  }
}
```

Read-only MCP tools are exposed by default:

- `tax_lookup`
- `tax_context`
- `tax_search`
- `tax_rubric`
- `tax_related`
- `tax_tags`
- `tax_tagged`
- `tax_template`
- `tax_stats`

The file-writing PDF tool is opt-in:

```bash
taxhelper-mcp --allow-write-tools
```

That exposes `tax_fill_pdf`, which requires an explicit `output_path`.

## Årsopgørelse Review Template

There is not a generic blank `årsopgørelse` to fill out. The annual assessment is
generated by Skattestyrelsen from reported and self-entered data. The closest
fill-out structure is `04.003 Oplysningsskemaet`, which contains the rubrik and
felt numbers used in TastSelv and on paper.

Generate a worksheet from the stored rubrikker:

```bash
taxhelper template
taxhelper template --editable-only
taxhelper template --tag befordring --include-guidance
taxhelper template --tag bolig --json
```

The command is read-only. The Markdown output is useful for human review; JSON is
intended for agents that need a structured checklist with source URLs, tags,
status, action hints, and blank fields for values/evidence.

## Filling The PDF

`taxhelper fill-pdf` fills a copy of `data/04003_januar2026-t.pdf` from JSON
values keyed by rubrik or felt number:

```json
{
  "rubrics": {
    "51": 12345,
    "53": 8000,
    "166": true
  }
}
```

```bash
taxhelper fill-pdf values.json --output filled-04003.pdf
```

Locked fields are skipped unless `--include-locked` is passed. The generated PDF
is a copy for review; check it manually before sending anything to
Skattestyrelsen.

## Source Policy

Prefer official material:

- `skat.dk` for citizen-facing guidance and current rates.
- `info.skat.dk` for Den juridiske vejledning and formal legal references.
- `retsinformation.dk` for laws and executive orders when a rule needs legal
  authority beyond guidance.

For every structured rule, store the source URL and keep amounts year-specific.
Do not overwrite older years when new rates are published.

## Open Source Notes

The code is MIT licensed. The project is independent and not affiliated with
Skattestyrelsen. The bundled PDF and fetched source material are official
Skattestyrelsen materials kept with source URLs for traceability; see `NOTICE`.

Project policies:

- `LICENSE`: MIT license for the code, including warranty/liability waiver.
- `DISCLAIMER.md`: no tax/legal/financial advice, no filing, no accuracy
  guarantee.
- `SECURITY.md`: private vulnerability reporting and data-safety guidance.
- `SUPPORT.md`: where to get technical help and what support is out of scope.
- `CODE_OF_CONDUCT.md`: participation rules for the public repository.
