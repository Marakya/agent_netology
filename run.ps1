param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/'
}
& uv --cache-dir .uv-cache sync --locked
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
Write-Host "Agent Netology: http://127.0.0.1:$Port"
& uv --cache-dir .uv-cache run uvicorn app.main:app --host 127.0.0.1 --port $Port
