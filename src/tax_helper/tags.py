from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TagDefinition:
    tag: str
    label: str
    description: str
    aliases: tuple[str, ...]
    patterns: tuple[str, ...]


@dataclass(frozen=True)
class TagAssignment:
    tag: str
    reason: str


TAG_DEFINITIONS: tuple[TagDefinition, ...] = (
    TagDefinition(
        "indkomst",
        "Indkomst",
        "Income fields and taxable receipts.",
        ("income", "earnings", "indtægt", "indtægter"),
        ("indkomst", "indtægt", "indtægter", "udbytte", "overskud"),
    ),
    TagDefinition(
        "personlig-indkomst",
        "Personlig indkomst",
        "Personal income fields.",
        ("personal-income", "personlig"),
        ("personlig indkomst",),
    ),
    TagDefinition(
        "kapitalindkomst",
        "Kapitalindkomst",
        "Capital income and capital income deductions.",
        ("capital-income", "kapital"),
        ("kapitalindkomst", "obligation", "pantebrev", "finansielle kontrakter"),
    ),
    TagDefinition(
        "fradrag",
        "Fradrag",
        "Deduction fields.",
        ("deduction", "deductions", "allowance", "allowances"),
        ("fradrag", "fradragsberettiget", "fradragsberettigede", "udgifter"),
    ),
    TagDefinition(
        "ligningsmaessige-fradrag",
        "Ligningsmæssige fradrag",
        "Assessment deductions such as commuting, union, gifts and travel.",
        ("ligningsmæssige fradrag", "assessment-deductions"),
        ("ligningsmæssige fradrag",),
    ),
    TagDefinition(
        "loen",
        "Løn",
        "Salary, wages, fees and employment income.",
        ("salary", "wage", "wages", "pay", "employment-income"),
        ("løn", "lønindkomst", "honorar", "bestyrelseshonorar", "arbejdsgiver"),
    ),
    TagDefinition(
        "personalegoder",
        "Personalegoder",
        "Employee benefits such as free car, phone, lodging and meals.",
        ("benefits", "employee-benefits", "perks"),
        ("personalegoder", "fri telefon", "fri bil", "fri kost", "fri logi", "firmabil"),
    ),
    TagDefinition(
        "fri-bil",
        "Fri bil",
        "Company car and free car tax treatment.",
        ("company-car", "firmabil", "car-benefit"),
        ("fri bil", "firmabil", "kørsel i fri bil"),
    ),
    TagDefinition(
        "pension",
        "Pension",
        "Pension schemes, pension contributions and pension income.",
        ("pensions", "retirement", "ratepension", "livrente"),
        ("pension", "ratepension", "livsvarige pensionsordninger", "aldersopsparing"),
    ),
    TagDefinition(
        "befordring",
        "Befordring",
        "Commuting and transport deduction.",
        ("commute", "commuting", "transport", "mileage", "kørsel", "koersel"),
        ("befordring", "kørselsfradrag", "transport", "kørsel"),
    ),
    TagDefinition(
        "rejse",
        "Rejse",
        "Work travel, food and lodging deductions.",
        ("travel", "lodging", "meals", "kost", "logi"),
        ("rejse", "rejseudgifter", "kost og logi", "logi", "småfornødenheder"),
    ),
    TagDefinition(
        "renter-gaeld",
        "Renter og gæld",
        "Interest income, interest expenses, debt and loans.",
        ("interest", "debt", "loan", "loans", "mortgage", "realkredit"),
        ("rente", "renteudgifter", "renteindtægter", "gæld", "realkredit", "studielån"),
    ),
    TagDefinition(
        "investering",
        "Investering",
        "Shares, bonds, investment funds, financial contracts and investor deductions.",
        ("investment", "investments", "stocks", "shares", "bonds", "aktier"),
        ("aktier", "obligationer", "investerings", "investorfradrag", "finansielle kontrakter"),
    ),
    TagDefinition(
        "bolig",
        "Bolig",
        "Home, property, dwelling and real-estate related fields.",
        ("home", "house", "property", "real-estate", "housing"),
        ("bolig", "helårsbolig", "ejerbolig", "fritidsbolig", "sommerhus", "ejendom"),
    ),
    TagDefinition(
        "udlejning",
        "Udlejning",
        "Rental income and property/asset letting.",
        ("rental", "rent", "letting", "lease"),
        ("udlejning", "udlejer", "lejeindtægt", "langtidsudleje", "værelsesudlejning"),
    ),
    TagDefinition(
        "boligfradrag",
        "Boligfradrag",
        "Service deduction and crafts deduction.",
        ("home-deduction", "servicefradrag", "håndværkerfradrag", "haandvaerkerfradrag"),
        ("servicefradrag", "håndværkerfradrag", "haandvaerkerfradrag"),
    ),
    TagDefinition(
        "virksomhed",
        "Virksomhed",
        "Business income, business scheme, accounting and business information.",
        ("business", "company", "self-employed", "self-employment"),
        ("virksomhed", "selvstændig", "virksomhedsordning", "kapitalafkastordning", "cvr"),
    ),
    TagDefinition(
        "regnskab",
        "Regnskab",
        "Accounting, financial statements and business accounts.",
        ("accounting", "accounts", "bookkeeping", "financial-statement"),
        ("regnskab", "vareforbrug", "afskrivninger", "balancen", "egenkapital", "nettoomsætning"),
    ),
    TagDefinition(
        "moms",
        "Moms",
        "VAT-related business information.",
        ("vat",),
        ("moms",),
    ),
    TagDefinition(
        "gaver",
        "Gaver",
        "Gifts and donations.",
        ("gifts", "donations", "charity"),
        ("gaver", "godkendte foreninger", "kultur- og forskningsinstitutioner"),
    ),
    TagDefinition(
        "fagforening-a-kasse",
        "Fagforening og A-kasse",
        "Union, unemployment insurance, early retirement and flex benefit deductions.",
        ("union", "unemployment", "a-kasse", "akasse"),
        ("fagligt kontingent", "a-kasse", "efterlønsordning", "fleksydelse"),
    ),
    TagDefinition(
        "bidrag",
        "Bidrag",
        "Maintenance, child support and related contributions.",
        ("support-payments", "child-support", "maintenance"),
        ("underholdsbidrag", "børnebidrag", "bidrag"),
    ),
    TagDefinition(
        "international",
        "International",
        "Foreign, cross-border or special international fields.",
        ("foreign", "abroad", "cross-border", "international"),
        ("udenland", "udenlandske", "gæstestuderende", "grænsegænger", "dis indkomst"),
    ),
    TagDefinition(
        "laast-felt",
        "Låst felt",
        "Fields shown as locked by the official form.",
        ("locked", "locked-field", "låst", "felt låst"),
        ("felt låst",),
    ),
    TagDefinition(
        "blanket",
        "Blanket",
        "Fields that refer to an official form/blanket.",
        ("form", "forms", "official-form"),
        ("blanket",),
    ),
    TagDefinition(
        "aabent-felt",
        "Åbent felt",
        "Fields not marked as locked in the PDF index.",
        ("open-field", "editable", "self-entered"),
        (),
    ),
)


def normalize_tag_query(value: str) -> str:
    value = normalize_text(value)
    value = value.replace("æ", "ae").replace("ø", "oe").replace("å", "aa")
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


TAG_BY_NAME = {definition.tag: definition for definition in TAG_DEFINITIONS}
ALIASES: dict[str, str] = {}
for definition in TAG_DEFINITIONS:
    ALIASES[definition.tag] = definition.tag
    ALIASES[normalize_tag_query(definition.label)] = definition.tag
    for alias in definition.aliases:
        ALIASES[normalize_tag_query(alias)] = definition.tag


def canonical_tag(value: str) -> str:
    normalized = normalize_tag_query(value)
    return ALIASES.get(normalized, normalized)


def matching_tags(value: str) -> list[str]:
    normalized = normalize_tag_query(value)
    matches = [
        tag
        for alias, tag in ALIASES.items()
        if normalized in alias or alias in normalized
    ]
    return sorted(set(matches))


def tag_rubric(
    *,
    code: str,
    section: str,
    label: str,
    locked_note: str,
    detail_title: str = "",
    detail_body: str = "",
) -> list[TagAssignment]:
    del detail_body
    text = normalize_text(" ".join([section, label, locked_note, detail_title]))
    assignments: dict[str, str] = {}
    for definition in TAG_DEFINITIONS:
        if definition.patterns and any(normalize_text(pattern) in text for pattern in definition.patterns):
            assignments[definition.tag] = "matched taxonomy pattern"

    if normalize_text(section).startswith("fradrag") or " fradrag " in f" {text} ":
        assignments["fradrag"] = "section or text mentions fradrag"
    if not normalize_text(locked_note):
        assignments["aabent-felt"] = "not marked locked in PDF index"
    if "felt låst" in text:
        assignments["laast-felt"] = "PDF index marks field as locked"
    if "blanket" in text:
        assignments["blanket"] = "PDF index or guidance references a blanket"
    if code in {"51"}:
        assignments["befordring"] = "known commuting rubrik"
    if code in {"53"}:
        assignments["rejse"] = "known travel deduction rubrik"
    if code in {"460", "461"}:
        assignments["boligfradrag"] = "known home/service deduction rubrik"

    return [
        TagAssignment(tag=tag, reason=assignments[tag])
        for tag in sorted(assignments)
        if tag in TAG_BY_NAME
    ]


def tag_rule(
    *,
    title: str,
    category: str,
    summary: str,
    applies_to: str,
    caveats: str,
) -> list[TagAssignment]:
    text = normalize_text(" ".join([title, category, summary, applies_to, caveats]))
    assignments: dict[str, str] = {}
    for definition in TAG_DEFINITIONS:
        if definition.patterns and any(normalize_text(pattern) in text for pattern in definition.patterns):
            assignments[definition.tag] = "matched taxonomy pattern"
    return [
        TagAssignment(tag=tag, reason=assignments[tag])
        for tag in sorted(assignments)
        if tag in TAG_BY_NAME
    ]
