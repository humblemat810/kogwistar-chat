param(
    [int]$Port = 5001,
    [string]$PythonExe = "",
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

# Run from the repository root so `main.py` and `static/` resolve correctly.
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    # Prefer the local virtual environment if it exists, otherwise fall back to
    # whatever `python` is on PATH.
    $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        $PythonExe = $venvPython
    }
    else {
        $PythonExe = "python"
    }
}

# These environment variables are the same ones read by `main.py`.
$env:CHAT_APP_PORT = "$Port"
if ([string]::IsNullOrWhiteSpace($env:CHAT_APP_URL)) {
    $env:CHAT_APP_URL = "http://localhost:$Port"
}

# Start the frontend in the background and keep the process handle.
$Process = Start-Process -FilePath $PythonExe -ArgumentList "main.py" -WorkingDirectory $Root -PassThru

# Wait until the HTTP port accepts connections before opening the browser.
$deadline = [DateTime]::UtcNow.AddSeconds(60)
while ([DateTime]::UtcNow -lt $deadline) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if ($async.AsyncWaitHandle.WaitOne(250)) {
            $client.EndConnect($async)
            $client.Close()
            break
        }
        $client.Close()
    }
    catch {
        Start-Sleep -Milliseconds 500
    }
    Start-Sleep -Milliseconds 250
}

if (-not $NoBrowser) {
    # Open the login page, not the root path, because the root redirects.
    Start-Process "http://localhost:$Port/login"
}

Write-Host "Frontend process started with PID $($Process.Id)"
Write-Host "Open http://localhost:$Port/login if the browser did not launch."
