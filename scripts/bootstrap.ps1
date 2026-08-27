<#
.SYNOPSIS
    Create a reproducible development environment for `portable`.

.DESCRIPTION
    Idempotent: safe to re-run. The POSIX equivalent is scripts/bootstrap.sh
    and the two must stay in step.

.PARAMETER NoNative
    Skip the compiled extension. This yields a complete, correct install --
    the pure-Python path is the reference implementation, not a degraded mode
    (ADR 0008).

.EXAMPLE
    .\scripts\bootstrap.ps1
    .\scripts\bootstrap.ps1 -NoNative
#>
[CmdletBinding()]
param([switch]$NoNative)

$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$Root = (Get-Location).Path
$Venv = if ($env:VENV) { $env:VENV } else { Join-Path $Root '.venv' }
$BuildNative = if ($NoNative) { 'OFF' } else { 'ON' }

$Py = if ($env:PYTHON) { $env:PYTHON } else { 'python' }
if (-not (Get-Command $Py -ErrorAction SilentlyContinue)) {
    throw "error: $Py not found. portable needs Python 3.11 or newer."
}

& $Py -c @"
import sys
if sys.version_info < (3, 11):
    sys.exit(f'error: Python 3.11+ required, found {sys.version.split()[0]}')
"@
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "==> virtualenv at $Venv"
if (-not (Test-Path $Venv)) { & $Py -m venv $Venv }
$Bin = Join-Path $Venv 'Scripts'

Write-Host '==> dependencies (pinned by constraints.txt)'
& (Join-Path $Bin 'python.exe') -m pip install --quiet --upgrade pip
& (Join-Path $Bin 'pip.exe') install --quiet -r requirements-dev.txt -c constraints.txt

if ($BuildNative -eq 'ON' -and -not (Get-Command cmake -ErrorAction SilentlyContinue)) {
    Write-Host '==> cmake not found; installing without the native extension.'
    Write-Host '    This is a complete, correct install -- the pure-Python path is the'
    Write-Host '    reference implementation, not a degraded mode (ADR 0008).'
    $BuildNative = 'OFF'
}

Write-Host "==> installing portable (editable, native=$BuildNative)"
$env:PORTABLE_BUILD_NATIVE = $BuildNative
& (Join-Path $Bin 'pip.exe') install --quiet -e . --no-build-isolation --no-deps

Write-Host '==> verifying'
& (Join-Path $Bin 'python.exe') -c @"
from portable_core import __version__, native
print(f'    portable_core {__version__}, native implementation: {native.implementation()}')
"@
& (Join-Path $Bin 'python.exe') -m portable_core.lint all | Out-Null
if ($LASTEXITCODE -eq 0) { Write-Host '    lint rules: clean' }

Write-Host @"

Ready. Activate with:

    $Venv\Scripts\Activate.ps1

Then:

    pt --help                  the portfolio tool
    make check                 everything CI runs
    make test-fast             the fast unit subset

A worked example is in examples\walkthrough.md.
"@
