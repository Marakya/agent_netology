param([int]$Port = 8501)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'Install uv first' }
& uv --cache-dir .uv-cache sync --locked
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& uv --cache-dir .uv-cache run streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port $Port
