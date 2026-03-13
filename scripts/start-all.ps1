# Inicia o stack completo: OpenHands + Controller API + Bot Telegram
# Execute da raiz do projeto: .\scripts\start-all.ps1

$Root = $PSScriptRoot ? (Split-Path $PSScriptRoot) : (Get-Location).Path

Write-Host "`n=== my-agent startup ===" -ForegroundColor Cyan

# 1. OpenHands (Docker)
$ohDir = Join-Path $Root "vendor\openhands"
$ohEnv = Join-Path $ohDir ".env"
if (-not (Test-Path $ohEnv)) {
    Write-Host "[OpenHands] Criando .env a partir do .env.example..." -ForegroundColor Yellow
    Copy-Item (Join-Path $ohDir ".env.example") $ohEnv
    Write-Host "[OpenHands] ATENÇÃO: edite $ohEnv com sua OPENHANDS_LLM_API_KEY antes de continuar." -ForegroundColor Red
    Write-Host "Pressione Enter após editar o arquivo..."
    Read-Host
}

Write-Host "[OpenHands] Iniciando container..." -ForegroundColor Green
Push-Location $ohDir
docker compose up -d
Pop-Location

# 2. Controller API (Python / uvicorn)
$ctrlDir = Join-Path $Root "apps\controller-api"
$ctrlEnv = Join-Path $ctrlDir ".env"
if (-not (Test-Path $ctrlEnv)) {
    Write-Host "[Controller] Criando .env a partir do .env.example..." -ForegroundColor Yellow
    Copy-Item (Join-Path $ctrlDir ".env.example") $ctrlEnv
    Write-Host "[Controller] ATENÇÃO: edite $ctrlEnv (CONTROLLER_API_KEY + TELEGRAM_BOT_TOKEN)." -ForegroundColor Red
    Write-Host "Pressione Enter após editar o arquivo..."
    Read-Host
}

Write-Host "[Controller] Iniciando FastAPI em background..." -ForegroundColor Green
$ctrlJob = Start-Process -FilePath "cmd.exe" `
    -ArgumentList "/c cd `"$ctrlDir`" && uvicorn main:app --host 127.0.0.1 --port 8000" `
    -PassThru -WindowStyle Normal

Write-Host "[Controller] PID $($ctrlJob.Id) — http://127.0.0.1:8000/docs"

# 3. Bot Telegram (Node)
$botDir = Join-Path $Root "apps\gateway-node"
$botEnv = Join-Path $botDir ".env"
if (-not (Test-Path $botEnv)) {
    Write-Host "[Bot] Criando .env a partir do .env.example..." -ForegroundColor Yellow
    Copy-Item (Join-Path $botDir ".env.example") $botEnv
    Write-Host "[Bot] ATENÇÃO: edite $botEnv com TELEGRAM_BOT_TOKEN." -ForegroundColor Red
    Write-Host "Pressione Enter após editar o arquivo..."
    Read-Host
}

Write-Host "[Bot] Iniciando gateway Telegram..." -ForegroundColor Green
Set-Location $botDir
npm start
