param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

# This launcher favors a reliable first-run experience over startup speed:
# dependency installation is idempotent and also picks up requirements updates.
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
}

& ".venv\Scripts\python.exe" -m pip install -r requirements.txt

$localUrl = "http://localhost:$Port"
Write-Host "Starting 肥鱼 FlowChat at $localUrl"
# Delay browser launch until Uvicorn has had time to bind the local port.
$browserJob = Start-Job -ScriptBlock {
    param($url)
    Start-Sleep -Seconds 2
    Start-Process -FilePath $url
} -ArgumentList $localUrl

try {
    & ".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port $Port
}
finally {
    Remove-Job -Job $browserJob -Force -ErrorAction SilentlyContinue
}
