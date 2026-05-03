#!/usr/bin/env sh
set -eu

REPO_URL="${TAXHELPER_REPO_URL:-https://github.com/rasmusrbj/taxhelper.git}"
PYTHON_BIN="${PYTHON:-}"

say() {
  printf '%s\n' "$*"
}

have() {
  command -v "$1" >/dev/null 2>&1
}

if [ -z "$PYTHON_BIN" ]; then
  if have python3; then
    PYTHON_BIN="python3"
  elif have python; then
    PYTHON_BIN="python"
  else
    say "error: Python 3.12+ is required, but no python3/python was found."
    exit 1
  fi
fi

if ! "$PYTHON_BIN" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
then
  say "error: Python 3.12+ is required."
  exit 1
fi

if ! have pipx && ! "$PYTHON_BIN" -m pipx --version >/dev/null 2>&1; then
  say "Installing pipx..."
  "$PYTHON_BIN" -m pip install --user pipx
  "$PYTHON_BIN" -m pipx ensurepath >/dev/null 2>&1 || true
fi

run_pipx() {
  if have pipx; then
    pipx "$@"
  else
    "$PYTHON_BIN" -m pipx "$@"
  fi
}

missing_poppler=""
for tool in pdftotext pdftohtml pdftocairo; do
  if ! have "$tool"; then
    missing_poppler="$missing_poppler $tool"
  fi
done

if [ -n "$missing_poppler" ] && [ "${TAXHELPER_SKIP_POPPLER:-0}" != "1" ]; then
  say "Installing Poppler tools for PDF scraping/filling..."
  if have brew; then
    brew install poppler
  elif have apt-get; then
    if have sudo; then
      sudo apt-get update
      sudo apt-get install -y poppler-utils
    else
      say "warning: sudo not found. Install poppler-utils manually."
    fi
  elif have dnf; then
    if have sudo; then
      sudo dnf install -y poppler-utils
    else
      say "warning: sudo not found. Install poppler-utils manually."
    fi
  else
    say "warning: missing Poppler tools:$missing_poppler"
    say "Install Poppler manually, or run taxhelper init --offline only after tools are available."
  fi
fi

say "Installing taxhelper from $REPO_URL ..."
run_pipx install --force "git+$REPO_URL"

say ""
say "taxhelper installed."
if ! have taxhelper; then
  say "If taxhelper is not on PATH yet, restart your shell or add ~/.local/bin to PATH."
fi
say ""
say "Next:"
say "  taxhelper init"
say "  taxhelper lookup 'field 417'"
