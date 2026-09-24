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
    Exits immediately if another tray icon for this install is already running.
#>
[CmdletBinding()]
param(
    # Install root. Derived from this script's location when not supplied.
    [string]$AppRoot = '',
    # .env to read the WebUI host, ports and TLS flag from.
    [string]$EnvFile = '',
    # How often to check whether SyntH is answering, in seconds.
    [int]$PollSeconds = 2
)

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

if (-not $AppRoot) {
    $AppRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}
if (-not $EnvFile) {
    $EnvFile = Join-Path $AppRoot '.env'
}
if ($PollSeconds -lt 1) { $PollSeconds = 1 }

# ---------------------------------------------------------------------------
# Only one tray icon per install: a second launch must not add a second icon.
# ---------------------------------------------------------------------------
$mutexName = 'SyntH-Tray-' + [Math]::Abs($AppRoot.ToLower().GetHashCode())
$mutex = New-Object System.Threading.Mutex($false, $mutexName)
if (-not $mutex.WaitOne(0)) { exit 0 }

# ---------------------------------------------------------------------------
# Where the WebUI is, read the same way the health check reads it.
# ---------------------------------------------------------------------------
function Read-DotEnv {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $value = $matches[2] -replace '\s+#.*$', ''
            $map[$matches[1]] = $value.Trim().Trim('"').Trim("'")
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
        Call the one launcher for start/stop/open. Runs off the UI thread's
        blocking path: these can take a little while, and the tray has to stay
        responsive while they do.
    #>
    param([string[]]$Arguments, [switch]$Wait)
    $python = Get-PythonPath -Console:(-not $Wait)
    if (-not $python) { return }
    $launcher = Join-Path $AppRoot 'scripts\start_synth.py'
    $params = @{
        FilePath     = $python
        ArgumentList = (@($launcher) + $Arguments)
        WindowStyle  = 'Hidden'
    }
    if ($Wait) { $params['Wait'] = $true }
    Start-Process @params
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
            return New-Object System.Drawing.Icon($path)
        }
    }
    return [System.Drawing.SystemIcons]::Application
}

$script:State = 'starting'
$script:ReadyUrl = $null

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = Get-TrayIcon
$notify.Visible = $true
$notify.Text = 'SyntH is starting'

$menu = New-Object System.Windows.Forms.ContextMenuStrip

$openItem = New-Object System.Windows.Forms.ToolStripMenuItem('Open SyntH')
$openItem.Font = New-Object System.Drawing.Font($openItem.Font, [System.Drawing.FontStyle]::Bold)
$openItem.Add_Click({
    # The launcher opens the WebUI when SyntH is up, and starts it when it is not.
    Invoke-Launcher -Arguments @('--no-tray')
}) | Out-Null
$menu.Items.Add($openItem) | Out-Null

$restartItem = New-Object System.Windows.Forms.ToolStripMenuItem('Restart')
$restartItem.Add_Click({
    $notify.Text = 'SyntH is restarting'
    Invoke-Launcher -Arguments @('--stop') -Wait
    Invoke-Launcher -Arguments @('--no-browser', '--no-tray')
}) | Out-Null
$menu.Items.Add($restartItem) | Out-Null

$shutdownItem = New-Object System.Windows.Forms.ToolStripMenuItem('Shut down')
$shutdownItem.Add_Click({
    $notify.Text = 'SyntH is shutting down'
    Invoke-Launcher -Arguments @('--stop') -Wait
    $script:State = 'stopped'
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
    $script:State = 'hidden'
    $notify.Visible = $false
    [System.Windows.Forms.Application]::ExitThread()
}) | Out-Null
$menu.Items.Add($quitItem) | Out-Null

$notify.ContextMenuStrip = $menu

# ---------------------------------------------------------------------------
# State poll: the tooltip and the balloon both follow it.
# ---------------------------------------------------------------------------
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = $PollSeconds * 1000
$timer.Add_Tick({
    $url = Test-SyntHIsUp
    if ($url) {
        if ($script:State -ne 'running') {
            $script:State = 'running'
            $script:ReadyUrl = $url
            $notify.Text = 'SyntH is running'
            # The install's own reassurance: the launcher window is long gone by now.
            $notify.ShowBalloonTip(5000, 'SyntH is running', "$url`nRight-click this icon for the menu.", [System.Windows.Forms.ToolTipIcon]::Info)
        }
        return
    }
    if ($script:State -eq 'running') {
        $script:State = 'stopped'
        $notify.Text = 'SyntH is stopped'
    }
})
$timer.Start()

# Announce the launch straight away: this is the whole point of the icon.
$notify.ShowBalloonTip(5000, 'SyntH is starting', 'It will be ready in a moment. Right-click this icon for the menu.', [System.Windows.Forms.ToolTipIcon]::Info)

try {
    [System.Windows.Forms.Application]::Run()
} finally {
    $timer.Stop()
    $notify.Visible = $false
    $notify.Dispose()
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
