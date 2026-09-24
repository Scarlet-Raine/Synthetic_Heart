<#
.SYNOPSIS
    Notification-area (tray) icon for a native Windows SyntH install.

.DESCRIPTION
    Started by scripts/start_synth.py. A native install launches SyntH as a
    windowless background process, so the launcher's window closes and the machine
    looks like it did nothing: the user is left staring at a desktop with no sign
    that anything started. This icon is that sign. It appears immediately, says it
    is starting, turns into "running" once the WebUI answers, and gives right-click
    access to the WebUI, a restart, a shutdown, and (later) an update.

    Everything it does is a call into the launcher, so there is one implementation
    of starting, stopping and opening SyntH: this file owns only the icon.

.NOTES
    Windows PowerShell 5.1 compatible. No dependencies beyond the .NET framework.
    Every step is appended to <AppRoot>\logs\tray.log, because a tray icon that does
    not appear has no other way of saying why. Exits immediately if another tray icon
    for this install is already running.
#>
[CmdletBinding()]
param(
    # Install root. Derived from this script's location when not supplied.
    [string]$AppRoot = '',
    # .env to read the WebUI host, ports and TLS flag from.
    [string]$EnvFile = '',
    # How often to check whether SyntH is answering, in seconds.
    [int]$PollSeconds = 2,
    # Where to append what happened. Defaults to <AppRoot>\logs\tray.log.
    [string]$LogFile = '',
    # Skip the "starting" balloon. The icon and its menu still appear.
    [switch]$NoBalloon
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Resolve the install first: the log lives under it, so a bad AppRoot is the one
# error that cannot be logged anywhere sensible.
# ---------------------------------------------------------------------------
try {
    if (-not $AppRoot) { $AppRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path }
    $AppRoot = [System.IO.Path]::GetFullPath($AppRoot)
} catch {
    Write-Error "synth_tray: cannot resolve the install directory: $($_.Exception.Message)"
    exit 2
}
if (-not $EnvFile) { $EnvFile = Join-Path $AppRoot '.env' }
if (-not $LogFile) { $LogFile = Join-Path $AppRoot 'logs\tray.log' }
if ($PollSeconds -lt 1) { $PollSeconds = 1 }

function Write-TrayLog {
    param([string]$Message, [string]$Level = 'INFO')
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    try {
        $dir = Split-Path -Parent $LogFile
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        Add-Content -LiteralPath $LogFile -Value "[$stamp] [$Level] $Message" -Encoding UTF8
    } catch { }
}

Write-TrayLog "--- tray start: pid=$PID app=$AppRoot ps=$($PSVersionTable.PSVersion) ---"

trap {
    # A tray that throws must say so: otherwise the user is left with an empty
    # notification area and no explanation anywhere. This is why the log exists.
    Write-TrayLog "FATAL $($_.Exception.GetType().Name): $($_.Exception.Message)" 'ERROR'
    Write-TrayLog ($_.ScriptStackTrace) 'ERROR'
    try { [System.Windows.Forms.Application]::ExitThread() } catch { }
    exit 1
}

# ---------------------------------------------------------------------------
# Where the WebUI is, read the same way the health check reads it.
# ---------------------------------------------------------------------------
function Read-DotEnv {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in (Get-Content -LiteralPath $Path)) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            # Snapshot the groups before anything else: -match and -notmatch both
            # overwrite the automatic $matches, so reading it after another regex
            # operation indexes into whatever ran last.
            $key = $matches[1]
            $value = $matches[2].Trim()
            # Keep a '#' when it is inside quotes; strip an inline comment otherwise.
            if ($value -notmatch '^\s*[''"]') { $value = ($value -split '\s+#')[0].Trim() }
            $map[$key] = $value.Trim('"').Trim("'")
        }
    }
    return $map
}

$settings = Read-DotEnv -Path $EnvFile
$tls = ($settings['SYNTH_WEBUI_TLS'] -eq '1') -or ($settings['SECURE_CONNECTION'] -eq '1')
$webHost = if ($settings['SYNTH_WEBUI_HOST']) { $settings['SYNTH_WEBUI_HOST'] } else { '127.0.0.1' }
$httpPort = if ($settings['SYNTH_WEBUI_HTTP_PORT']) { $settings['SYNTH_WEBUI_HTTP_PORT'] } else { '8080' }
$httpsPort = if ($settings['SYNTH_WEBUI_HTTPS_PORT']) { $settings['SYNTH_WEBUI_HTTPS_PORT'] } else { '8000' }

if ($tls) {
    $script:PrimaryUrl = 'https://{0}:{1}/' -f $webHost, $httpsPort
    $script:OtherUrl = 'http://{0}:{1}/' -f $webHost, $httpPort
} else {
    $script:PrimaryUrl = 'http://{0}:{1}/' -f $webHost, $httpPort
    $script:OtherUrl = 'https://{0}:{1}/' -f $webHost, $httpsPort
}
Write-TrayLog "env: $EnvFile (exists: $(Test-Path -LiteralPath $EnvFile)); tls=$tls; urls: $($script:PrimaryUrl), $($script:OtherUrl)"

# ---------------------------------------------------------------------------
# WinForms. Reported on its own line: a missing assembly is a different problem
# from a tray that cannot reach the app.
# ---------------------------------------------------------------------------
try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    Write-TrayLog 'WinForms loaded'
} catch {
    Write-TrayLog "FATAL cannot load WinForms: $($_.Exception.Message)" 'ERROR'
    exit 3
}

# ---------------------------------------------------------------------------
# Only one tray icon per install: a second launch must not add a second icon.
# ---------------------------------------------------------------------------
$mutexName = 'Local\SyntH-Tray-' + [Math]::Abs($AppRoot.ToLower().GetHashCode())
$mutex = New-Object System.Threading.Mutex($false, $mutexName)
$owned = $false
try {
    $owned = $mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    $owned = $true
}
if (-not $owned) {
    Write-TrayLog "another tray already owns '$mutexName'; exiting" 'WARN'
    exit 0
}
Write-TrayLog "mutex '$mutexName' acquired"

function Get-PythonPath {
    param([switch]$Console)
    $names = if ($Console) { @('python.exe', 'pythonw.exe') } else { @('pythonw.exe', 'python.exe') }
    foreach ($name in $names) {
        $candidate = Join-Path $AppRoot (Join-Path '.venv\Scripts' $name)
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

function Invoke-Launcher {
    <#
        Call the one launcher for start/stop/open. These can take a little while,
        so the waiting ones use the console interpreter (which can be waited on)
        and the rest use the windowless one.
    #>
    param([string[]]$Arguments, [switch]$Wait, [string]$Description = 'launch')
    $python = Get-PythonPath -Console:(-not $Wait)
    if (-not $python) {
        Write-TrayLog "action '$Description': no interpreter under $AppRoot\.venv\Scripts" 'ERROR'
        return
    }
    $launcher = Join-Path $AppRoot 'scripts\start_synth.py'
    Write-TrayLog "action '$Description': $python $launcher $($Arguments -join ' ')"
    $params = @{
        FilePath     = $python
        ArgumentList = (@($launcher) + $Arguments)
        WindowStyle  = 'Hidden'
    }
    if ($Wait) { $params['Wait'] = $true }
    try {
        Start-Process @params
    } catch {
        Write-TrayLog "action '$Description' failed: $($_.Exception.Message)" 'ERROR'
    }
}

function Test-SyntHIsUp {
    # Returns the URL that answered, or $null.
    foreach ($url in @($script:PrimaryUrl, $script:OtherUrl)) {
        try {
            $request = [System.Net.HttpWebRequest]::Create($url)
            $request.Method = 'GET'
            $request.Timeout = 1500
            $request.ReadWriteTimeout = 1500
            $request.ServerCertificateValidationCallback = { $true }
            $request.AllowAutoRedirect = $true
            $response = $request.GetResponse()
            $response.Close()
            return $url
        } catch {
            continue
        }
    }
    return $null
}

# ---------------------------------------------------------------------------
# The icon
# ---------------------------------------------------------------------------
function Get-TrayIcon {
    foreach ($name in @('synth-tray.ico', 'synth.ico')) {
        $path = Join-Path $AppRoot (Join-Path 'installer' $name)
        if (Test-Path -LiteralPath $path) {
            try {
                $icon = New-Object System.Drawing.Icon($path)
                Write-TrayLog "icon: installer\$name ($($icon.Width)x$($icon.Height))"
                return $icon
            } catch {
                Write-TrayLog "icon installer\$name failed to load: $($_.Exception.Message)" 'WARN'
            }
        } else {
            Write-TrayLog "icon candidate missing: installer\$name" 'WARN'
        }
    }
    Write-TrayLog 'falling back to the built-in application icon' 'WARN'
    return [System.Drawing.SystemIcons]::Application
}

$script:State = 'starting'
$script:ReadyUrl = $null

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = Get-TrayIcon
$notify.Visible = $true
$notify.Text = 'SyntH is starting'
Write-TrayLog 'notification icon created and made visible'

$menu = New-Object System.Windows.Forms.ContextMenuStrip

$openItem = New-Object System.Windows.Forms.ToolStripMenuItem('Open SyntH')
$openItem.Font = New-Object System.Drawing.Font($openItem.Font, [System.Drawing.FontStyle]::Bold)
$openItem.Add_Click({
    # The launcher opens the WebUI when SyntH is up, and starts it when it is not.
    Invoke-Launcher -Arguments @('--no-tray') -Description 'open the WebUI'
}) | Out-Null
$menu.Items.Add($openItem) | Out-Null

$restartItem = New-Object System.Windows.Forms.ToolStripMenuItem('Restart')
$restartItem.Add_Click({
    try {
        $notify.Text = 'SyntH is restarting'
        Invoke-Launcher -Arguments @('--stop') -Wait -Description 'restart: stop'
        Invoke-Launcher -Arguments @('--no-browser', '--no-tray') -Description 'restart: start again'
    } catch {
        Write-TrayLog "restart failed: $($_.Exception.Message)" 'ERROR'
    }
}) | Out-Null
$menu.Items.Add($restartItem) | Out-Null

$shutdownItem = New-Object System.Windows.Forms.ToolStripMenuItem('Shut down')
$shutdownItem.Add_Click({
    try {
        $notify.Text = 'SyntH is shutting down'
        Invoke-Launcher -Arguments @('--stop') -Wait -Description 'shut down'
        $script:State = 'stopped'
    } catch {
        Write-TrayLog "shutdown failed: $($_.Exception.Message)" 'ERROR'
    }
}) | Out-Null
$menu.Items.Add($shutdownItem) | Out-Null

$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null

# Placeholder so the shape of the menu is already right: updating an install is
# not implemented yet, and a greyed entry says that better than a missing one.
$updateItem = New-Object System.Windows.Forms.ToolStripMenuItem('Check for updates')
$updateItem.Enabled = $false
$updateItem.ToolTipText = 'Not implemented yet'
$menu.Items.Add($updateItem) | Out-Null

$quitItem = New-Object System.Windows.Forms.ToolStripMenuItem('Hide this icon')
$quitItem.ToolTipText = 'SyntH keeps running'
$quitItem.Add_Click({
    Write-TrayLog 'action: hide requested'
    $script:State = 'hidden'
    $notify.Visible = $false
    [System.Windows.Forms.Application]::ExitThread()
}) | Out-Null
$menu.Items.Add($quitItem) | Out-Null

$notify.ContextMenuStrip = $menu
$notify.Add_MouseDoubleClick({ $openItem.PerformClick() })
Write-TrayLog 'menu built: open / restart / shut down / check for updates (disabled) / hide'

# ---------------------------------------------------------------------------
# State poll: the tooltip and the balloon both follow it.
# ---------------------------------------------------------------------------
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = $PollSeconds * 1000
$timer.Add_Tick({
    try {
        $url = Test-SyntHIsUp
        if ($url) {
            if ($script:State -ne 'running') {
                $script:State = 'running'
                $script:ReadyUrl = $url
                $notify.Text = 'SyntH is running'
                Write-TrayLog "state: running ($url)"
                # The install's own reassurance: the launcher window is long gone.
                $notify.ShowBalloonTip(5000, 'SyntH is running', "$url`nRight-click this icon for the menu.", [System.Windows.Forms.ToolTipIcon]::Info)
            }
            return
        }
        if ($script:State -eq 'running') {
            $script:State = 'stopped'
            $notify.Text = 'SyntH is stopped'
            Write-TrayLog 'state: stopped'
        }
    } catch {
        # Never let a failed probe kill the icon; the next tick tries again.
        Write-TrayLog "state check failed: $($_.Exception.Message)" 'WARN'
    }
})
$timer.Start()

# Announce the launch straight away: this is the whole point of the icon.
if (-not $NoBalloon) {
    try {
        $notify.ShowBalloonTip(5000, 'SyntH is starting', 'It will be ready in a moment. Right-click this icon for the menu.', [System.Windows.Forms.ToolTipIcon]::Info)
        Write-TrayLog 'start balloon shown'
    } catch {
        Write-TrayLog "start balloon failed: $($_.Exception.Message)" 'WARN'
    }
}

Write-TrayLog "entering the message loop (icon visible: $($notify.Visible))"

try {
    [System.Windows.Forms.Application]::Run()
} finally {
    Write-TrayLog 'message loop ended'
    try { $timer.Stop() } catch { }
    try { $notify.Visible = $false; $notify.Dispose() } catch { }
    try { $mutex.ReleaseMutex(); $mutex.Dispose() } catch { }
}
