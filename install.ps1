param(
    [string]$RepoUrl = $env:TAXHELPER_REPO_URL,
    [switch]$SkipPoppler
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

if (-not $RepoUrl) {
    $RepoUrl = "https://github.com/rasmusrbj/taxhelper.git"
}

function Write-Step {
    param([string]$Message)
    Write-Host $Message
}

function Test-Command {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Get-BaseArgs {
    param([string[]]$Command)
    if ($Command.Count -le 1) {
        return @()
    }
    return $Command[1..($Command.Count - 1)]
}

function Test-PythonCommand {
    param([string[]]$Command)
    $exe = $Command[0]
    $baseArgs = Get-BaseArgs $Command
    & $exe @baseArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" *> $null
    return ($LASTEXITCODE -eq 0)
}

function Find-Python {
    if ($env:PYTHON) {
        $candidate = @($env:PYTHON)
        if (Test-PythonCommand $candidate) {
            return $candidate
        }
    }
    if (Test-Command "py") {
        foreach ($version in @("-3.12", "-3")) {
            $candidate = @("py", $version)
            if (Test-PythonCommand $candidate) {
                return $candidate
            }
        }
    }
    foreach ($name in @("python", "python3")) {
        if (Test-Command $name) {
            $candidate = @($name)
            if (Test-PythonCommand $candidate) {
                return $candidate
            }
        }
    }
    throw "Python 3.12+ is required. Install Python first, then rerun this installer."
}

$script:PythonCommand = Find-Python

function Invoke-Python {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$PythonArgs)
    $exe = $script:PythonCommand[0]
    $baseArgs = Get-BaseArgs $script:PythonCommand
    & $exe @baseArgs @PythonArgs
}

function Test-PipxModule {
    Invoke-Python -m pipx --version *> $null
    return ($LASTEXITCODE -eq 0)
}

function Invoke-Pipx {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$PipxArgs)
    if (Test-Command "pipx") {
        & pipx @PipxArgs
    } else {
        Invoke-Python -m pipx @PipxArgs
    }
}

function Add-ToCurrentPath {
    param([string]$Directory)
    if ($Directory -and (Test-Path $Directory) -and ($env:Path -notlike "*$Directory*")) {
        $env:Path = "$Directory;$env:Path"
    }
}

function Find-Taxhelper {
    $command = Get-Command "taxhelper" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $names = @("taxhelper.exe", "taxhelper.cmd", "taxhelper")
    $roots = @()
    if ($env:PIPX_BIN_DIR) {
        $roots += $env:PIPX_BIN_DIR
    }
    if ($env:USERPROFILE) {
        $roots += (Join-Path $env:USERPROFILE ".local\bin")
    }
    if ($env:APPDATA) {
        $roots += (Join-Path $env:APPDATA "Python\Scripts")
        $roots += Get-ChildItem -Path (Join-Path $env:APPDATA "Python") -Directory -Filter "Python*" -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName "Scripts" }
    }

    foreach ($root in $roots) {
        foreach ($name in $names) {
            $path = Join-Path $root $name
            if (Test-Path $path) {
                return $path
            }
        }
    }
    return $null
}

if (-not (Test-Command "pipx") -and -not (Test-PipxModule)) {
    Write-Step "Installing pipx..."
    Invoke-Python -m pip install --user pipx
}

Invoke-Pipx ensurepath *> $null
if ($env:PIPX_BIN_DIR) {
    Add-ToCurrentPath $env:PIPX_BIN_DIR
}
if ($env:USERPROFILE) {
    Add-ToCurrentPath (Join-Path $env:USERPROFILE ".local\bin")
}
if ($env:APPDATA) {
    Add-ToCurrentPath (Join-Path $env:APPDATA "Python\Scripts")
}

$missingPoppler = @()
foreach ($tool in @("pdftotext", "pdftohtml", "pdftocairo")) {
    if (-not (Test-Command $tool)) {
        $missingPoppler += $tool
    }
}

if ($missingPoppler.Count -gt 0 -and -not $SkipPoppler -and $env:TAXHELPER_SKIP_POPPLER -ne "1") {
    Write-Step "Installing Poppler tools for PDF scraping/filling..."
    if (Test-Command "winget") {
        winget install --id oschwartz10612.Poppler -e --accept-source-agreements --accept-package-agreements
    } elseif (Test-Command "choco") {
        choco install poppler -y
    } elseif (Test-Command "scoop") {
        scoop install poppler
    } else {
        Write-Warning "Missing Poppler tools: $($missingPoppler -join ', ')"
        Write-Warning "Install Poppler manually, then reopen PowerShell before running PDF commands."
    }
}

Write-Step "Installing taxhelper from $RepoUrl ..."
Invoke-Pipx install --force "git+$RepoUrl"

Write-Step "Installing taxhelper agent skill for Codex and Claude Code..."
$taxhelper = Find-Taxhelper
if ($taxhelper) {
    & $taxhelper install-skills --force
} else {
    Write-Warning "Could not auto-install the agent skill."
    Write-Warning "Run 'taxhelper install-skills --force' after restarting PowerShell."
}

Write-Step ""
Write-Step "taxhelper installed."
if (-not (Get-Command "taxhelper" -ErrorAction SilentlyContinue)) {
    Write-Step "If taxhelper is not on PATH yet, restart PowerShell."
}
Write-Step ""
Write-Step "Next:"
Write-Step "  taxhelper init"
Write-Step "  taxhelper lookup 'field 417'"
