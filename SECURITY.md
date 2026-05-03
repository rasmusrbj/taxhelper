# Security Policy

## Supported Versions

The supported version is the current `main` branch and the latest tagged release,
if releases exist. Older commits are best-effort only.

## Reporting A Vulnerability

Please do not open a public issue for a vulnerability that could expose local
files, personal data, credentials, or unsafe command execution.

Preferred reporting path:

- Use GitHub's private vulnerability reporting / Security Advisories feature if
  it is enabled for the repository.
- If private reporting is not available, open a minimal public issue saying that
  you have a security report and avoid exploit details or sensitive data.

Include:

- Affected command or module.
- Steps to reproduce using dummy data.
- Expected impact.
- Your environment: OS, Python version, and `taxhelper --version`.

## Data Safety

`taxhelper` is designed to keep data local in SQLite and local files. Do not put
real CPR numbers, MitID credentials, bank statements, employer documents, or full
tax returns into GitHub issues, pull requests, logs, screenshots, or examples.

Generated PDFs and SQLite databases can contain personal tax information. Treat
them as private files and do not commit them.

## Scope

In scope:

- Unsafe file writes outside requested paths.
- Command injection in scraper/fill workflows.
- Unexpected network requests or leakage of local data.
- Malicious or malformed source files that crash or escape expected handling.

Out of scope:

- General tax-rule accuracy disputes.
- Vulnerabilities in Skattestyrelsen websites, Poppler, Python, pipx, GitHub, or
  operating-system package managers. Report those to the relevant project or
  authority.
