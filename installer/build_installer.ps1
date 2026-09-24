<#
.SYNOPSIS
    Build the SyntH Windows installer with Inno Setup.

.DESCRIPTION
    Compiles installer\synth-installer.iss into
    installer\Output\SyntH-Setup-<version>.exe.

    Inno Setup 6 must be installed. If pgvector has not been vendored under
    installer\vendor\pgvector\ the installer still builds, but semantic memory
    search will be off on machines it installs to; the script says so. Run the
    "pgvector for Windows" GitHub workflow to produce those files.

.PARAMETER SkipVendorWarning
    Do not warn about a missing installer\vendor\pgvector.

.EXAMPLE
    installer\build_installer.ps1
#>

[CmdletBinding()]
param(
    [string]$Version,
    [switch]$SkipVendorWarning
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$iss = Join-Path $PSScriptRoot 'synth-installer.iss'

function Resolve-Version {
    # The repository has no version file: GitVersion (GitVersion.yml) is the
    # source of truth and CI passes its output to this script. Locally, fall back
    # to the newest tag, then to a placeholder so a build always succeeds.
    param([string]$Explicit)

    if ($Explicit) { return $Explicit }

    $fromGitVersion = Get-Command dotnet-gitversion -ErrorAction SilentlyContinue
    if ($fromGitVersion) {
        try {
            $json = & dotnet-gitversion | ConvertFrom-Json
            if ($json.MajorMinorPatch) { return $json.MajorMinorPatch }
        } catch { }
    }

    try {
        $tag = (& git -C $repoRoot describe --tags --abbrev=0 2>$null)
        if ($LASTEXITCODE -eq 0 -and $tag) { return ($tag -replace '^v', '') }
    } catch { }

    return '0.0.0-dev'
}

$version = Resolve-Version -Explicit $Version
$output = Join-Path $PSScriptRoot "Output\SyntH-Setup-$version.exe"

function Find-Iscc {
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
    ) | Where-Object { $_ -and (Test-Path $_) }
    if ($candidates) { return $candidates[0] }
    return $null
}

$iscc = Find-Iscc
if (-not $iscc) {
    Write-Host 'Inno Setup 6 was not found.' -ForegroundColor Red
    Write-Host '  Install it from https://jrsoftware.org/isdl.php (or: winget install JRSoftware.InnoSetup)'
    Write-Host '  then run this script again.'
    exit 1
}

if (-not (Test-Path $iss)) { Write-Host "missing $iss" -ForegroundColor Red; exit 1 }

$vendor = Join-Path $PSScriptRoot 'vendor\pgvector'
if (-not (Test-Path $vendor) -and -not $SkipVendorWarning) {
    Write-Host 'note: installer\vendor\pgvector is absent.' -ForegroundColor Yellow
    Write-Host '      The installer will still build, but installed copies get no vector'
    Write-Host '      extension, so SOUL memory search runs in memory-only mode.'
    Write-Host '      Produce the files with the "pgvector for Windows" workflow.'
    Write-Host ''
}

Write-Host "Building SyntH $version with Inno Setup" -ForegroundColor Cyan
Write-Host "  compiler: $iscc"
Write-Host "  script:   $iss"
Write-Host ''

& $iscc "/DAppVersion=$version" $iss
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host ''
    Write-Host "Inno Setup failed with exit code $code" -ForegroundColor Red
    exit $code
}

if (Test-Path $output) {
    $size = [math]::Round((Get-Item $output).Length / 1MB, 1)
    Write-Host ''
    Write-Host "Built $output ($size MB)" -ForegroundColor Green
} else {
    Write-Host ''
    Write-Host "Inno Setup reported success but $output is missing" -ForegroundColor Yellow
    exit 1
}
