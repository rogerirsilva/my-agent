# my-agent

MVP de agente pessoal com Telegram, Controller API (FastAPI) e OpenHands como executor de IA.

## Estrutura

```
my-agent/
├── apps/
│   ├── gateway-node/        # Bot Telegram (Node.js + Telegraf)
│   └── controller-api/      # API de controle, políticas e orquestração (Python + FastAPI)
├── vendor/
│   └── openhands/           # Docker Compose para OpenHands (agente de IA)
├── data/                    # Runtime: tasks.json, audit.log, policy.yaml (não commitado)
└── scripts/
    ├── start-all.ps1        # Start completo (Windows)
    └── start-all.sh         # Start completo (Linux/Mac)
```

## Pré-requisitos

- Node.js >= 18
- Python >= 3.11
- Docker Desktop (rodando)
- Token do Telegram Bot ([BotFather](https://t.me/BotFather))
- API key de um LLM (Anthropic, OpenAI, etc.) — para o OpenHands

## Setup rápido (Windows)

```powershell
# 1. Clone e entre na pasta
git clone https://github.com/rogerirsilva/my-agent
cd my-agent

# 2. Instale dependências Node
cd apps\gateway-node && npm install && cd ..\..

# 3. Instale dependências Python
cd apps\controller-api && pip install -r requirements.txt && cd ..\..

# 4. Configure os .env (NÃO commite tokens)
copy apps\gateway-node\.env.example apps\gateway-node\.env
copy apps\controller-api\.env.example apps\controller-api\.env
copy vendor\openhands\.env.example vendor\openhands\.env
# Edite cada .env com as chaves reais

# 5. Inicie tudo
.\scripts\start-all.ps1
```

## Comandos do Bot Telegram

| Comando | Descrição |
|---------|-----------|
| `/start` | Apresentação |
| `/status` | Info do host (CPU, RAM, uptime) |
| `/exec <secret> <comando>` | Executa comando no host (via controller + aprovação) |
| `/think <tarefa>` | Envia tarefa para o OpenHands (agente de IA) |
| `/tasks` | Lista as últimas tarefas |

### Fluxo de aprovação

```
Você → /exec abc123 whoami
Bot  → "Aguarda aprovação..." + botões [✅ Aprovar] [❌ Rejeitar]
Você → clica ✅
Bot  → executa e responde com output
```

## Variáveis de ambiente

### apps/gateway-node/.env
| Variável | Descrição |
|----------|-----------|
| `TELEGRAM_BOT_TOKEN` | Token do BotFather |
| `ALLOWED_TELEGRAM_USER_ID` | Seu user ID (padrão: 210623701) |
| `BOT_SHARED_SECRET` | Senha para /exec |
| `CONTROLLER_API_URL` | URL do controller (padrão: http://127.0.0.1:8000) |
| `CONTROLLER_API_KEY` | Chave de autenticação do controller |

### apps/controller-api/.env
| Variável | Descrição |
|----------|-----------|
| `CONTROLLER_API_KEY` | Deve bater com o bot |
| `TELEGRAM_BOT_TOKEN` | Para notificar resultado do OpenHands |
| `OPENHANDS_URL` | URL do OpenHands (padrão: http://127.0.0.1:3000) |

### vendor/openhands/.env
| Variável | Descrição |
|----------|-----------|
| `OPENHANDS_LLM_API_KEY` | API key do LLM (Anthropic, OpenAI, etc.) |
| `OPENHANDS_LLM_MODEL` | Modelo (ex: anthropic/claude-3-5-sonnet-20241022) |

## Política de segurança

O arquivo `data/policy.yaml` é criado automaticamente na primeira execução e pode ser editado.
Por padrão, `/exec` requer aprovação manual e bloqueia comandos destrutivos (rm -rf, format, etc.).

## Próximos passos

- [ ] WhatsApp Business Cloud API (gateway adicional)
- [ ] Autenticação multi-usuário
- [ ] Dashboard web (FastAPI Admin ou similar)
