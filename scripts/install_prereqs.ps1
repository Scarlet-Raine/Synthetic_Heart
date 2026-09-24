<#
.SYNOPSIS
    Provision everything a native SyntH install needs on Windows, without admin.

.DESCRIPTION
    Installs, entirely inside the user profile:

      * uv              - the Python package manager, which brings its own Python,
                          so there is no Python prerequisite and nothing is added
                          to the system PATH
      * PostgreSQL      - the official EnterpriseDB Windows binaries, unpacked
                          into the install directory and run as a private cluster
                          on a free port (no Windows service, no admin)
      * pgvector        - the vector extension needed for semantic memory search,
                          copied from installer\vendor\pgvector\pg<major>\ which
                          the release build populates (see vendor\README.md)
      * ffmpeg          - optional (-WithFfmpeg); only needed for local audio
                          conversion, not for cloud engines
      * Node.js         - optional (-WithNode); only needed for the Minecraft
                          vessel bridge

    It never installs a Windows service, never writes outside the install
    directory and %TEMP%, and never requires elevation.

    Afterwards scripts\bootstrap.py does the database, the .env and the Python
    environment; pass -RunBootstrap to chain straight into it.

.PARAMETER InstallDir
    Where SyntH lives. Defaults to $env:LOCALAPPDATA\Programs\SyntH.

.PARAMETER PostgresVersion
    The EnterpriseDB binary release to fetch, e.g. '16.10-1'.

.PARAMETER WithFfmpeg
    Also download a static ffmpeg build into the install directory.

.PARAMETER WithNode
    Also install Node.js (Minecraft vessel bridge only).

.PARAMETER RunBootstrap
    Run scripts\bootstrap.py at the end (database, .env, dependencies).

.PARAMETER SkipPostgres
    Assume a PostgreSQL server is already available and only install uv.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_prereqs.ps1 -RunBootstrap

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_prereqs.ps1 -WithFfmpeg -WithNode
#>

[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\SyntH'),
    [string]$PostgresVersion = '16.10-1',
    [switch]$WithFfmpeg,
    [switch]$WithNode,
    [switch]$RunBootstrap,
    [switch]$SkipPostgres,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
# PS 5.1 renders a progress bar per chunk on a 300 MB download; turning it off is
# the difference between two minutes and twenty.
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$script:RepoRoot = Split-Path -Parent $PSScriptRoot
$script:Warnings = New-Object System.Collections.Generic.List[string]
$script:LogFile = Join-Path $env:TEMP 'synth_prereqs.log'

function Write-Log([string]$Text) {
    Add-Content -Path $script:LogFile -Value "[$(Get-Date -Format 'HH:mm:ss')] $Text" -Encoding UTF8 -ErrorAction SilentlyContinue
}
function Write-Step([string]$Text) { Write-Log $Text; if (-not $Quiet) { Write-Host $Text -ForegroundColor Cyan } }
function Write-Ok([string]$Text)   { Write-Log "  ok: $Text"; if (-not $Quiet) { Write-Host "  ok: $Text" -ForegroundColor Green } }
function Write-Note([string]$Text) { Write-Log "      $Text"; if (-not $Quiet) { Write-Host "      $Text" -ForegroundColor DarkGray } }
function Write-Warn2([string]$Text) {
    $script:Warnings.Add($Text)
    Write-Log "  warning: $Text"
    Write-Host "  warning: $Text" -ForegroundColor Yellow
}
function Fail([string]$Text) { Write-Log "  ERROR: $Text"; Write-Host "  ERROR: $Text" -ForegroundColor Red; exit 1 }

function Get-Archive([string]$Url, [string]$Destination) {
    # WebClient rather than Invoke-WebRequest: no progress bar, far faster in PS 5.1.
    $client = New-Object System.Net.WebClient
    $client.Headers.Add('User-Agent', 'SyntH-installer')
    try { $client.DownloadFile($Url, $Destination) } finally { $client.Dispose() }
}

function Ensure-Uv {
    $uvPath = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path $uvPath) {
        Write-Ok "uv already installed ($(& $uvPath --version))"
        return $uvPath
    }
    $existing = Get-Command uv -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Ok "uv already installed ($(& uv --version))"
        return $existing.Source
    }

    Write-Note 'installing uv from astral.sh (user scope, no admin)'
    $installer = Join-Path $env:TEMP 'synth-uv-install.ps1'
    Get-Archive 'https://astral.sh/uv/install.ps1' $installer
    & powershell -NoProfile -ExecutionPolicy Bypass -File $installer | Out-Null
    Remove-Item $installer -ErrorAction SilentlyContinue

    if (Test-Path $uvPath) { Write-Ok "uv installed at $uvPath"; return $uvPath }
    $found = Get-Command uv -ErrorAction SilentlyContinue
    if ($found) { Write-Ok "uv installed ($($found.Source))"; return $found.Source }
    Fail 'uv was installed but could not be located; open a new terminal and re-run'
}

function Ensure-Postgres {
    param([string]$TargetRoot)

    $pgRoot = Join-Path $TargetRoot 'pgsql'
    $psql = Join-Path $pgRoot 'bin\psql.exe'
    if (Test-Path $psql) {
        Write-Ok "PostgreSQL already provisioned ($((& $psql --version) -replace '\s+', ' '))"
        return $pgRoot
    }

    $zipName = "postgresql-$PostgresVersion-windows-x64-binaries.zip"
    $url = "https://get.enterprisedb.com/postgresql/$zipName"
    $zipPath = Join-Path $env:TEMP $zipName

    New-Item -ItemType Directory -Force -Path $TargetRoot | Out-Null
    if (Test-Path $zipPath) {
        Write-Note "using the already downloaded $zipName"
    } else {
        Write-Note "downloading $zipName (about 320 MB, one time)"
        try { Get-Archive $url $zipPath }
        catch { Fail "could not download $url - $($_.Exception.Message)" }
    }

    Write-Note 'unpacking PostgreSQL'
    $staging = Join-Path $env:TEMP ("synth-pg-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    try {
        Expand-Archive -Path $zipPath -DestinationPath $staging -Force
        $inner = Join-Path $staging 'pgsql'
        if (-not (Test-Path $inner)) { Fail 'the downloaded archive did not contain a pgsql folder' }
        Copy-Item -Path $inner -Destination $TargetRoot -Recurse -Force
    } finally {
        Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
    }

    if (-not (Test-Path $psql)) { Fail 'PostgreSQL was unpacked but psql.exe is missing' }
    Write-Ok "PostgreSQL provisioned ($((& $psql --version) -replace '\s+', ' '))"
    return $pgRoot
}

function Ensure-Pgvector {
    # Copies the pgvector files the release build puts under
    # installer\vendor\pgvector\pg<major>\. pgvector publishes no prebuilt Windows
    # binaries, so these come from our own build (see vendor\README.md).
    param([string]$PgRoot)

    $libDir = Join-Path $PgRoot 'lib'
    $shareDir = Join-Path $PgRoot 'share\extension'
    if (Test-Path (Join-Path $libDir 'vector.dll')) {
        Write-Ok 'pgvector already present'
        return $true
    }

    $psql = Join-Path $PgRoot 'bin\psql.exe'
    $major = $null
    if (Test-Path $psql) {
        $raw = (& $psql --version)
        if ($raw -match '(\d+)\.') { $major = $Matches[1] }
    }
    if (-not $major) {
        Write-Warn2 'could not determine the PostgreSQL major version; skipping pgvector'
        return $false
    }

    $vendor = Join-Path $RepoRoot "installer\vendor\pgvector\pg$major"
    if (-not (Test-Path $vendor)) {
        Write-Warn2 ("pgvector is not bundled for PostgreSQL $major; semantic memory search " +
            'will be off until it is installed. See installer\vendor\README.md')
        return $false
    }

    New-Item -ItemType Directory -Force -Path $libDir, $shareDir | Out-Null
    $copied = 0
    foreach ($file in @(Get-ChildItem -Path $vendor -Filter 'vector.dll' -Recurse -ErrorAction SilentlyContinue)) {
        Copy-Item $file.FullName (Join-Path $libDir 'vector.dll') -Force; $copied++
    }
    foreach ($file in @(Get-ChildItem -Path $vendor -Filter 'vector*.control' -Recurse -ErrorAction SilentlyContinue)) {
        Copy-Item $file.FullName (Join-Path $shareDir $file.Name) -Force; $copied++
    }
    foreach ($file in @(Get-ChildItem -Path $vendor -Filter 'vector--*.sql' -Recurse -ErrorAction SilentlyContinue)) {
        Copy-Item $file.FullName (Join-Path $shareDir $file.Name) -Force; $copied++
    }

    if (Test-Path (Join-Path $libDir 'vector.dll')) {
        Write-Ok "pgvector installed for PostgreSQL $major ($copied file(s))"
        return $true
    }
    Write-Warn2 "bundled pgvector files for PostgreSQL $major did not include vector.dll"
    return $false
}

function Ensure-Ffmpeg {
    param([string]$TargetRoot)

    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) { Write-Ok 'ffmpeg already on PATH'; return }
    $local = Join-Path $TargetRoot 'ffmpeg\bin\ffmpeg.exe'
    if (Test-Path $local) { Write-Ok 'ffmpeg already provisioned'; return }

    $zipPath = Join-Path $env:TEMP 'synth-ffmpeg.zip'
    Write-Note 'downloading ffmpeg (about 90 MB)'
    try { Get-Archive 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' $zipPath }
    catch { Write-Warn2 "could not download ffmpeg: $($_.Exception.Message)"; return }

    $staging = Join-Path $env:TEMP ("synth-ffmpeg-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    try {
        Expand-Archive -Path $zipPath -DestinationPath $staging -Force
        $exe = Get-ChildItem -Path $staging -Filter 'ffmpeg.exe' -Recurse -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $exe) { Write-Warn2 'ffmpeg.exe was not found in the downloaded archive'; return }
        $target = Join-Path $TargetRoot 'ffmpeg\bin'
        New-Item -ItemType Directory -Force -Path $target | Out-Null
        Copy-Item (Join-Path $exe.DirectoryName '*') $target -Recurse -Force
        Write-Ok "ffmpeg installed at $target"
    } finally {
        Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
    }
}

function Ensure-Node {
    if (Get-Command node -ErrorAction SilentlyContinue) { Write-Ok 'Node.js already on PATH'; return }

    $msi = Join-Path $env:TEMP 'synth-node-lts-x64.msi'
    $url = 'https://nodejs.org/dist/lts/node-lts-x64.msi'
    Write-Note 'downloading Node.js LTS (about 30 MB)'
    try { Get-Archive $url $msi } catch { Write-Warn2 "could not download Node.js: $($_.Exception.Message)"; return }

    # MSI installs go to Program Files; that needs admin, so tell the user instead
    # of silently failing.
    $process = Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /quiet /norestart" -Wait -PassThru
    if ($process.ExitCode -eq 0 -or $process.ExitCode -eq 3010) {
        Write-Ok 'Node.js installed'
    } else {
        Write-Warn2 ("Node.js needs an elevated install (msiexec exit $($process.ExitCode)); " +
            'the Minecraft vessel will stay unavailable until it is installed')
    }
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
Write-Log '=== SyntH prerequisites ==='
Write-Step 'Preparing SyntH dependencies'
Write-Note "install directory: $InstallDir"
Write-Note "log: $script:LogFile"

$uvPath = Ensure-Uv

$pgRoot = $null
if ($SkipPostgres) {
    Write-Note 'skipping PostgreSQL (-SkipPostgres)'
} else {
    $pgRoot = Ensure-Postgres -TargetRoot $InstallDir
    Ensure-Pgvector -PgRoot $pgRoot | Out-Null
}

if ($WithFfmpeg) { Ensure-Ffmpeg -TargetRoot $InstallDir }
if ($WithNode) { Ensure-Node }

$ffmpegBin = Join-Path $InstallDir 'ffmpeg\bin'
if (-not (Test-Path (Join-Path $ffmpegBin 'ffmpeg.exe'))) { $ffmpegBin = $null }

# Record what was provisioned so bootstrap and the uninstaller can find it.
$stateDir = Join-Path $InstallDir 'data'
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
$state = [ordered]@{
    provisioned_at = (Get-Date).ToString('o')
    install_dir    = $InstallDir
    uv             = $uvPath
    postgres_root  = $pgRoot
    postgres_bin   = if ($pgRoot) { Join-Path $pgRoot 'bin' } else { $null }
    ffmpeg_bin     = $ffmpegBin
}
$state | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $stateDir 'prereqs.json') -Encoding UTF8

if ($RunBootstrap) {
    Write-Step 'Setting up the database and the Python environment'
    $bootstrapArgs = @('run', '--no-project', 'python', (Join-Path $RepoRoot 'scripts\bootstrap.py'), '--portable')
    if ($pgRoot) { $bootstrapArgs += @('--pg-bin', (Join-Path $pgRoot 'bin')) }
    if ($ffmpegBin) { $env:PATH = "$ffmpegBin;$env:PATH" }
    & $uvPath @bootstrapArgs
    if ($LASTEXITCODE -ne 0) { Fail "bootstrap failed with exit code $LASTEXITCODE" }
}

if (-not $Quiet) {
    Write-Host ''
    Write-Host 'Dependencies ready.' -ForegroundColor Green
    Write-Host '  Start SyntH from the Start Menu shortcut, or run: uv run main.py'
    if ($script:Warnings.Count -gt 0) {
        Write-Host ''
        foreach ($warning in $script:Warnings) { Write-Host "  note: $warning" -ForegroundColor Yellow }
    }
}
Write-Log '=== done ==='
exit 0
