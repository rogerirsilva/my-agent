"""
Cliente OpenHands para o Controller API.

API verificada na versão 0.28:
  POST   /api/settings                — configura LLM (obrigatório antes de criar conversas)
  GET    /api/settings                — verifica se settings existem
  POST   /api/conversations           — cria conversa (campo: initial_user_msg)
  GET    /api/conversations/{id}      — status: RUNNING | STOPPED
  GET    /api/conversations/{id}/trajectory — dict {"trajectory": [...eventos]}
  GET    /health                      — health check

Inicia o servidor com:
  cd vendor/openhands && docker compose up -d
"""

import os
import json
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

OPENHANDS_URL          = os.getenv("OPENHANDS_URL", "http://127.0.0.1:3000").rstrip("/")
OPENHANDS_LLM_KEY      = os.getenv("OPENHANDS_LLM_API_KEY", "")
OPENHANDS_LLM_MODEL    = os.getenv("OPENHANDS_LLM_MODEL", "groq/llama-3.3-70b-versatile")
OPENHANDS_LLM_BASE_URL = os.getenv("OPENHANDS_LLM_BASE_URL", "")

# URL local do Ollama (acessível do host; dentro do Docker usa http://ollama:11434)
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")

# Arquivo de configuração de runtime (persistido entre requisições)
_CONFIG_FILE = Path(os.getenv("DATA_DIR", "../../data")).resolve() / "llm_config.json"

_UNAVAILABLE_MSG = (
    "OpenHands inacessível em {url}.\n"
    "Inicie com:\n"
    "  cd vendor/openhands\n"
    "  docker compose up -d\n"
    "Depois aguarde ~30s e tente novamente."
)


def _is_available() -> bool:
    try:
        r = requests.get(f"{OPENHANDS_URL}/health", timeout=5)
        return r.ok
    except requests.exceptions.ConnectionError:
        return False


def list_ollama_models() -> list[str]:
    """Retorna modelos instalados no Ollama, excluindo modelos de embedding."""
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        if r.ok:
            # Exclui modelos de embedding (nomic-embed, etc.)
            skip = {"nomic-embed", "mxbai-embed", "all-minilm", "embed"}
            return [
                m["name"] for m in r.json().get("models", [])
                if not any(s in m["name"].lower() for s in skip)
            ]
    except Exception:
        pass
    return []


def load_llm_config() -> dict:
    """Lê configuração LLM do arquivo de runtime. Retorna dict vazio se não existir."""
    try:
        if _CONFIG_FILE.exists():
            return json.loads(_CONFIG_FILE.read_text("utf-8"))
    except Exception:
        pass
    return {}


def save_llm_config(model: str, api_key: str, base_url: str) -> None:
    """Persiste configuração LLM no arquivo de runtime."""
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(
        json.dumps({"model": model, "api_key": api_key, "base_url": base_url}, indent=2),
        "utf-8",
    )


def _resolve_model() -> tuple[str, str, str]:
    """
    Resolve modelo, api_key e base_url efetivos.
    Prioridade: 1) llm_config.json (runtime) 2) env vars 3) auto-detect Ollama.
    Retorna (model, api_key, base_url).
    """
    cfg      = load_llm_config()
    model    = cfg.get("model")    or OPENHANDS_LLM_MODEL
    api_key  = cfg.get("api_key")  or OPENHANDS_LLM_KEY
    base_url = cfg.get("base_url") or OPENHANDS_LLM_BASE_URL

    # Auto-detecta modelo Ollama
    if model == "ollama/auto":
        available = list_ollama_models()
        if not available:
            model = "ollama/llama3.2"  # fallback; será erro se não instalado
        else:
            # Prefere modelos de código, cai para o primeiro disponível
            preferred = ["qwen2.5-coder", "qwen2.5", "codellama", "llama3", "llama3.2", "mistral"]
            model = next(
                (f"ollama/{m}" for p in preferred for m in available if p in m.lower()),
                f"ollama/{available[0]}",
            )

    # Para qualquer modelo ollama, ajusta base_url e api_key automaticamente
    if model.startswith("ollama/"):
        if not base_url:
            # host.docker.internal: alcança Ollama nativo no Windows/Mac via Docker Desktop
            base_url = "http://host.docker.internal:11434"
        if not api_key:
            api_key = "ollama"

    return model, api_key, base_url


def _ensure_settings() -> str | None:
    """
    Sempre empurra as settings de LLM para o OpenHands.
    Garante que modelo/chave/base_url atuais estejam configurados.
    Retorna None se OK, mensagem de erro se falhar.
    """
    model, api_key, base_url = _resolve_model()

    is_ollama = model.startswith("ollama/")
    if not is_ollama and not api_key:
        return (
            "OPENHANDS_LLM_API_KEY não configurado no controller .env.\n"
            "Adicione a chave Groq/Gemini ou configure OPENHANDS_LLM_MODEL=ollama/auto e reinicie."
        )

    payload = {
        "llm_model": model,
        "llm_api_key": api_key,
        "llm_base_url": base_url,
        "agent": "CodeActAgent",
        "language": "pt",
        "enable_default_condenser": True,
    }
    r = requests.post(f"{OPENHANDS_URL}/api/settings", json=payload, timeout=15)
    if not r.ok:
        return f"Falha ao configurar settings do OpenHands: {r.status_code} {r.text[:200]}"
    return None


def _extract_output(events: list, max_chars: int = 3500) -> str:
    """Extrai mensagens relevantes do agente a partir da lista de eventos."""
    lines = []
    for e in events:
        src    = e.get("source", "")
        action = e.get("action", "")
        obs    = e.get("observation", "")
        # Mensagem final de conclusão
        if src == "agent" and action == "finish":
            msg = e.get("args", {}).get("thought") or e.get("message", "")
            if msg:
                lines.append(f"✅ {msg}")
        # Comandos executados
        elif src == "agent" and action == "run":
            cmd = e.get("args", {}).get("command", "")
            if cmd:
                lines.append(f"$ {cmd}")
        # Resultado de comandos
        elif src == "environment" and obs == "run":
            out    = e.get("content", "")
            code   = e.get("extras", {}).get("exit_code", "?")
            if out:
                lines.append(f"[exit {code}]\n{out[:1000]}")
        # Respostas de texto do agente
        elif src == "agent" and action == "message":
            msg = e.get("args", {}).get("content", "") or e.get("message", "")
            if msg:
                lines.append(msg)

    return "\n\n".join(lines)[:max_chars] if lines else "(sem output)"


def run_task_sync(prompt: str, timeout_sec: int = 300) -> dict:
    """
    Submete uma tarefa ao OpenHands e aguarda o resultado (polling REST).

    Fluxo:
      1. Verifica disponibilidade + configura settings se necessário
      2. POST /api/conversations → obtém conversation_id
      3. Polling GET /api/conversations/{id} até status == STOPPED
      4. GET /api/conversations/{id}/trajectory → extrai output do agente

    Retorna dict com chave 'output' (str) ou 'error' (str).
    """
    if not _is_available():
        return {"error": _UNAVAILABLE_MSG.format(url=OPENHANDS_URL)}

    err = _ensure_settings()
    if err:
        return {"error": err}

    try:
        # 1. Cria conversa
        resp = requests.post(
            f"{OPENHANDS_URL}/api/conversations",
            json={"initial_user_msg": prompt},
            timeout=30,
        )
        resp.raise_for_status()
        data    = resp.json()
        conv_id = data.get("conversation_id")
        if not conv_id:
            return {"error": f"OpenHands não retornou conversation_id: {data}"}

        # 2. Polling até STOPPED — trata RATE_LIMITED e ERROR explicitamente
        deadline           = time.time() + timeout_sec
        poll_interval      = 8
        rate_limited_since = None

        while time.time() < deadline:
            time.sleep(poll_interval)
            try:
                status_resp = requests.get(
                    f"{OPENHANDS_URL}/api/conversations/{conv_id}",
                    timeout=30,
                )
            except requests.exceptions.Timeout:
                continue
            if not status_resp.ok:
                continue

            status = status_resp.json().get("status", "")

            if status == "STOPPED":
                break

            elif status == "ERROR":
                # Erro definitivo — lê trajectory para dar detalhes
                traj = requests.get(
                    f"{OPENHANDS_URL}/api/conversations/{conv_id}/trajectory",
                    timeout=30,
                )
                raw    = traj.json() if traj.ok else {}
                events = raw.get("trajectory", raw) if isinstance(raw, dict) else raw
                output = _extract_output(events if isinstance(events, list) else [])
                return {
                    "error": "OpenHands encerrou com ERROR.",
                    "output": output or "(sem output)",
                    "conv_id": conv_id,
                    "state": "ERROR",
                }

            elif status == "RATE_LIMITED":
                # Rate limit temporário — OpenHands retentará sozinho.
                # Extende o deadline em até 120s para dar tempo de recuperar.
                if rate_limited_since is None:
                    rate_limited_since = time.time()
                    deadline = max(deadline, time.time() + 120)
                # Volta a verificar a cada 15s enquanto rate-limited
                time.sleep(max(0, min(15, deadline - time.time())))
                continue

        else:
            if rate_limited_since:
                return {
                    "error": (
                        "⚠️ Rate limit do Groq atingido e não recuperado.\n"
                        "Use /setmodel para trocar para Ollama LOCAL (sem limites de taxa)."
                    ),
                    "conv_id": conv_id,
                    "state": "RATE_LIMITED",
                }
            return {
                "error": f"Timeout: OpenHands não concluiu em {timeout_sec}s.",
                "conv_id": conv_id,
                "state": "TIMEOUT",
            }

        # 3. Extrai output da trajectory (com retry)
        for attempt in range(3):
            try:
                traj_resp = requests.get(
                    f"{OPENHANDS_URL}/api/conversations/{conv_id}/trajectory",
                    timeout=60,
                )
                if traj_resp.ok:
                    break
            except requests.exceptions.Timeout:
                time.sleep(5)
        else:
            return {"error": "Falha ao obter trajectory após 3 tentativas", "conv_id": conv_id}

        raw    = traj_resp.json()
        events = raw.get("trajectory", raw) if isinstance(raw, dict) else raw
        output = _extract_output(events if isinstance(events, list) else [])

        return {"output": output, "conv_id": conv_id, "state": "STOPPED"}

    except Exception as exc:
        return {"error": str(exc)}
        return {"error": _UNAVAILABLE_MSG.format(url=OPENHANDS_URL)}

    try:
        # 1. Cria conversa
        resp = requests.post(
            f"{OPENHANDS_URL}/api/conversations",
            # Campo correto validado no schema OpenAPI 0.28
            json={"initial_user_msg": prompt},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        conv_id = data.get("conversation_id")
        if not conv_id:
            return {"error": f"OpenHands não retornou conversation_id: {data}"}

        # 2. Polling até STOPPED (o agente pode demorar 30-120s dependendo da tarefa)
        deadline = time.time() + timeout_sec
        poll_interval = 8   # segundos entre cada check
        while time.time() < deadline:
            time.sleep(poll_interval)
            try:
                status_resp = requests.get(
                    f"{OPENHANDS_URL}/api/conversations/{conv_id}",
                    timeout=30,   # timeout generoso; o server pode demorar sozinho
                )
            except requests.exceptions.Timeout:
                continue  # tenta de novo no próximo ciclo
            if not status_resp.ok:
                continue
            conv_data = status_resp.json()
            # ConversationStatus enum: "RUNNING" | "STOPPED"
            if conv_data.get("status") == "STOPPED":
                break
        else:
            return {
                "error": f"Timeout: OpenHands não concluiu em {timeout_sec}s.",
                "conv_id": conv_id,
            }

        # 3. Extrai output da trajectory (com retry se demorar)
        traj_resp = None
        for attempt in range(3):
            try:
                traj_resp = requests.get(
                    f"{OPENHANDS_URL}/api/conversations/{conv_id}/trajectory",
                    timeout=60,
                )
                if traj_resp.ok:
                    break
            except requests.exceptions.Timeout:
                time.sleep(5)
        if traj_resp is None or not traj_resp.ok:
            return {"error": "Falha ao obter trajectory após 3 tentativas", "conv_id": conv_id}
        if not traj_resp.ok:
            return {"error": f"Falha ao obter trajectory ({traj_resp.status_code})", "conv_id": conv_id}

        events = traj_resp.json()  # lista de eventos
        # Filtra mensagens do agente (type == "message", source == "agent")
        agent_messages = [
            e.get("message") or e.get("content") or ""
            for e in (events if isinstance(events, list) else [])
            if e.get("source") == "agent" or e.get("role") == "assistant"
        ]
        output = "\n\n".join(m for m in agent_messages if m)
        if not output:
            # fallback: retorna último evento de qualquer tipo
            output = str(events[-1]) if events else "(sem output)"

        return {"output": output, "conv_id": conv_id, "state": "STOPPED"}

    except Exception as exc:
        return {"error": str(exc)}
