# LMS Sync installer for Windows (PowerShell):
#   irm https://raw.githubusercontent.com/Pulkit-123/lms-sync-app/main/install.ps1 | iex
# Installs into %USERPROFILE%\LMS-Sync (set LMS_SYNC_DIR to change), adds a Desktop shortcut
# and a daily 8:00 sync (set LMS_SYNC_NO_DAILY=1 to skip). Safe to run again: your data stays.
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $Repo = if ($env:LMS_SYNC_UPDATE_REPO) { $env:LMS_SYNC_UPDATE_REPO } else { 'Pulkit-123/lms-sync-app' }
    $Api = if ($env:LMS_SYNC_UPDATE_API) { $env:LMS_SYNC_UPDATE_API } else { 'https://api.github.com' }
    $Dir = if ($env:LMS_SYNC_DIR) { $env:LMS_SYNC_DIR } else { Join-Path $env:USERPROFILE 'LMS-Sync' }
    $Headers = @{ 'User-Agent' = 'lms-sync-installer' }

    if (Test-Path (Join-Path $Dir '.git')) { throw "$Dir is a development copy (a git checkout); not touching it." }

    $Python = $null
    foreach ($candidate in @(@('py', '-3'), @('python'), @('python3'))) {
        $exe = $candidate[0]
        $pre = @($candidate | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $found = & $exe @pre -c "import sys, venv; print(sys.executable if sys.version_info >= (3, 9) else '')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $found) { $Python = $found.Trim(); break }
        } catch { }
    }
    if (-not $Python) {
        throw "Python 3.9 or newer is needed. Install it with 'winget install Python.Python.3.12' (or from python.org), open a new PowerShell window and run this again."
    }

    $Tmp = Join-Path ([IO.Path]::GetTempPath()) ("lms-sync-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $Tmp | Out-Null
    try {
        Write-Host 'Downloading the latest release...'
        $release = Invoke-RestMethod -Uri "$Api/repos/$Repo/releases/latest" -Headers $Headers -UseBasicParsing
        $zip = Join-Path $Tmp 'app.zip'
        Invoke-WebRequest -Uri $release.zipball_url -Headers $Headers -OutFile $zip -UseBasicParsing
        # .NET directly: Expand-Archive breaks in Windows PowerShell when PowerShell 7 is also installed.
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($zip, (Join-Path $Tmp 'x'))
        $top = Get-ChildItem (Join-Path $Tmp 'x') -Directory | Select-Object -First 1
        if (-not (Test-Path (Join-Path $top.FullName 'src\cli.py'))) { throw "The download doesn't look like LMS Sync." }
        New-Item -ItemType Directory -Path $Dir -Force | Out-Null
        Copy-Item -Path (Join-Path $top.FullName '*') -Destination $Dir -Recurse -Force
    } finally {
        Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue
    }

    Write-Host 'Setting up Python. This downloads about 60 MB, so it can take several minutes on slow Wi-Fi...'
    & $Python -m venv (Join-Path $Dir '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Creating the Python environment failed.' }
    $VenvPython = Join-Path $Dir '.venv\Scripts\python.exe'
    & $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $Dir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Installing the dependencies failed.' }

    $extra = @()
    if ($env:LMS_SYNC_NO_DAILY) { $extra = @('--no-daily') }
    & $VenvPython (Join-Path $Dir 'src\cli.py') install-integration @extra
    if ($LASTEXITCODE -ne 0) { Write-Warning "Some shortcuts couldn't be created." }

    Write-Host ''
    Write-Host "Installed in $Dir. Open it any time with 'LMS Sync' on your Desktop."
    Write-Host 'The page that opens walks you through adding the Chrome extension and logging in.'
    if (-not $env:LMS_SYNC_NO_START) {
        Start-Process -FilePath (Join-Path $Dir '.venv\Scripts\pythonw.exe') -ArgumentList ('"' + (Join-Path $Dir 'src\launcher.py') + '"')
    }
}
