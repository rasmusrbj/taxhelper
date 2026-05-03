---
name: taxhelper
description: Use when answering questions about Danish annual tax assessment notices (årsopgørelse), Skattestyrelsen rubrikker/felter, TastSelv guidance, Danish tax return review templates, or the taxhelper CLI/MCP server. Prefer taxhelper for source-linked lookup, SQLite-backed search, rubric tagging, PDF worksheet/fill flows, and agent retrieval context; do not use it as tax advice or for filing a return.
---

# Tax Helper

## Overview

Use `taxhelper` to retrieve source-linked context from a local SQLite database of
Danish tax rules, rubrikker, tags, and the bundled 04.003 PDF. Treat every answer
as research support, not tax advice, and point users back to official sources or
professional advice for decisions.

## First Checks

Use MCP tools first if a `taxhelper` MCP server is configured. Otherwise use the
CLI with `--json` for stable output.

```bash
taxhelper stats --json
```

If the database is missing or empty, run:

```bash
taxhelper init
```

For offline environments, use:

```bash
taxhelper init --offline
```

## Lookup Workflow

Resolve exact rubrik or field references before writing explanations:

```bash
taxhelper lookup "field 417" --json
taxhelper rubric 51 --json
taxhelper context "kan jeg få kørselsfradrag?" --json --limit 3 --max-chars 1000
```

Use tags to narrow broad questions:

```bash
taxhelper tags --json
taxhelper search "renteudgifter" --tag rente --json
taxhelper template --tag befordring --editable-only --json
```

When answering, include the relevant rubrik/felt, year, lock/blanket status,
short guidance summary, and source URL when available. State uncertainty clearly
when the retrieved data is incomplete.

## MCP Tool Map

Prefer these MCP tools when available:

- `tax_lookup`: resolve natural-language, rubrik, or field queries.
- `tax_context`: retrieve compact source-linked context for a user question.
- `tax_search`: search rubrikker, structured rules, and sources.
- `tax_rubric`: fetch one full rubrik record.
- `tax_related`: find rubrikker with shared taxonomy tags.
- `tax_tags` and `tax_tagged`: inspect and use taxonomy filters.
- `tax_template`: generate a review worksheet.
- `tax_stats`: inspect database coverage.

Only use `tax_fill_pdf` when the MCP server was explicitly started with
`--allow-write-tools` and the user has asked to create a filled PDF copy.

## Review And PDF Flow

Generate a worksheet before filling the PDF:

```bash
taxhelper template --editable-only --include-guidance --json
```

Fill a local copy of the bundled official 04.003 PDF only from explicit values:

```bash
taxhelper fill-pdf values.json --output filled-04003.pdf
```

Do not imply that the filled PDF files a return. It is a local review artifact.

## Safety Rules

- Do not present retrieved text as personalized tax advice.
- Do not invent thresholds, rates, deadlines, rubrik numbers, or source URLs.
- Use `taxhelper init --refresh` before claiming data is current.
- Use read-only commands for agent lookups; mutating commands are `init`, `seed`,
  `scrape-rubrics`, `import-url`, `rebuild-fts`, and `rebuild-tags`.
- Mention that official Skattestyrelsen/TastSelv systems remain authoritative.
