SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
    tag TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS entity_tags (
    entity_key TEXT NOT NULL,
    entity_kind TEXT NOT NULL CHECK (entity_kind IN ('rubric', 'rule', 'source')),
    entity_id TEXT NOT NULL,
    tax_year INTEGER,
    tag TEXT NOT NULL REFERENCES tags(tag) ON DELETE CASCADE,
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (entity_key, tag)
);

CREATE INDEX IF NOT EXISTS idx_entity_tags_tag ON entity_tags(tag);
CREATE INDEX IF NOT EXISTS idx_entity_tags_entity ON entity_tags(entity_kind, entity_id, tax_year);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL DEFAULT '',
    retrieved_at TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'guidance',
    effective_year INTEGER,
    body TEXT NOT NULL,
    checksum TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS source_fts USING fts5(
    source_id UNINDEXED,
    title,
    url,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS rules (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    tax_year INTEGER,
    summary TEXT NOT NULL,
    applies_to TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL,
    source_title TEXT NOT NULL DEFAULT '',
    source_publisher TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    caveats TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_rules_category ON rules(category);
CREATE INDEX IF NOT EXISTS idx_rules_tax_year ON rules(tax_year);

CREATE VIRTUAL TABLE IF NOT EXISTS rule_fts USING fts5(
    rule_id UNINDEXED,
    title,
    category,
    summary,
    applies_to,
    caveats,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS amounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    tax_year INTEGER NOT NULL,
    label TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    threshold_note TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_amounts_rule_id ON amounts(rule_id);
CREATE INDEX IF NOT EXISTS idx_amounts_tax_year ON amounts(tax_year);

CREATE TABLE IF NOT EXISTS return_fields (
    code TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('rubrik', 'felt')),
    tax_form TEXT NOT NULL CHECK (tax_form IN ('aarsopgoerelse', 'forskudsopgoerelse')),
    label TEXT NOT NULL,
    category TEXT NOT NULL,
    rule_id TEXT REFERENCES rules(id) ON DELETE SET NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_return_fields_rule_id ON return_fields(rule_id);
CREATE INDEX IF NOT EXISTS idx_return_fields_kind ON return_fields(kind);

CREATE TABLE IF NOT EXISTS rubrics (
    tax_year INTEGER NOT NULL,
    code TEXT NOT NULL,
    field_no TEXT NOT NULL DEFAULT '',
    form_code TEXT NOT NULL DEFAULT '',
    form_title TEXT NOT NULL DEFAULT '',
    section TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL,
    locked_note TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    detail_url TEXT NOT NULL DEFAULT '',
    detail_title TEXT NOT NULL DEFAULT '',
    detail_body TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tax_year, code)
);

CREATE INDEX IF NOT EXISTS idx_rubrics_code ON rubrics(code);
CREATE INDEX IF NOT EXISTS idx_rubrics_field_no ON rubrics(field_no);
CREATE INDEX IF NOT EXISTS idx_rubrics_section ON rubrics(section);

CREATE VIRTUAL TABLE IF NOT EXISTS rubric_fts USING fts5(
    tax_year UNINDEXED,
    code,
    field_no,
    section,
    label,
    locked_note,
    detail_title,
    detail_body,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""
