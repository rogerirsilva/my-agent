#!/usr/bin/env pwsh
# Baixa modelos recomendados para o Ollama.
# Pré-requisito: Ollama rodando (docker compose up -d em vendor/openhands)
#
# Uso:
#   .\scripts\pull-ollama-models.ps1              # baixa modelo padrão: qwen2.5:7b
#   .\scripts\pull-ollama-models.ps1 -Model llama3.2
#   .\scripts\pull-ollama-models.ps1 -All         # baixa todos os recomendados

param(
    [string]$Model = "qwen2.5:7b",
    [switch]$All
)

$ErrorActionPreference = "Stop"

# Verifica se o container ollama está rodando
$running = docker ps --filter "name=ollama" --format "{{.Names}}" 2>$null
if (-not $running) {
    Write-Host "❌ Container 'ollama' não está rodando." -ForegroundColor Red
    Write-Host "   Inicie com:  cd vendor\openhands ; docker compose up -d" -ForegroundColor Yellow
    exit 1
}

function Pull-Model {
    param([string]$name)
    Write-Host ""
    Write-Host "⬇️  Baixando $name ..." -ForegroundColor Cyan
    docker exec ollama ollama pull $name
    if ($LASTEXITCODE -eq 0) {
        Write-Host "✅ $name instalado com sucesso." -ForegroundColor Green
    } else {
        Write-Host "⚠️  Falha ao baixar $name." -ForegroundColor Yellow
    }
}

if ($All) {
    # Modelos recomendados em ordem de prioridade
    @(
        "qwen2.5:7b",         # Melhor custo-benefício para tarefas gerais + código (4.5 GB)
        "qwen2.5-coder:7b",   # Especializado em código (4.5 GB)
        "llama3.2:3b"         # Leve e rápido para testes (2 GB)
    ) | ForEach-Object { Pull-Model $_ }
} else {
    Pull-Model $Model
}

Write-Host ""
Write-Host "📋 Modelos instalados:" -ForegroundColor Cyan
docker exec ollama ollama list

Write-Host ""
Write-Host "💡 Para usar um modelo, edite apps\controller-api\.env:" -ForegroundColor Yellow
Write-Host "   OPENHANDS_LLM_MODEL=ollama/<nome-do-modelo>" -ForegroundColor White
Write-Host "   OPENHANDS_LLM_BASE_URL=http://ollama:11434" -ForegroundColor White
Write-Host "   OPENHANDS_LLM_API_KEY=ollama" -ForegroundColor White
Write-Host ""
Write-Host "   Ou use ollama/auto para detectar automaticamente o melhor modelo disponível." -ForegroundColor White
