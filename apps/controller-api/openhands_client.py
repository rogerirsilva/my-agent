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
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

OPENHANDS_URL     = os.getenv("OPENHANDS_URL", "http://127.0.0.1:3000").rstrip("/")
OPENHANDS_LLM_KEY = os.getenv("OPENHANDS_LLM_API_KEY", "")
OPENHANDS_LLM_MODEL = os.getenv("OPENHANDS_LLM_MODEL", "groq/llama-3.3-70b-versatile")

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


def _ensure_settings() -> str | None:
    """Garante que as settings de LLM estejam salvas. Retorna None se OK, mensagem de erro se falhar."""
    check = requests.get(f"{OPENHANDS_URL}/api/settings", timeout=10)
    if check.status_code == 200:
        return None  # já configurado

    if not OPENHANDS_LLM_KEY:
        return (
            "OPENHANDS_LLM_API_KEY não configurado no controller .env.\n"
            "Adicione a chave Groq/Gemini e reinicie o controller."
        )

    payload = {
        "llm_model": OPENHANDS_LLM_MODEL,
        "llm_api_key": OPENHANDS_LLM_KEY,
        "llm_base_url": "",
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

        # 2. Polling até STOPPED
        deadline      = time.time() + timeout_sec
        poll_interval = 8
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
            if status_resp.json().get("status") == "STOPPED":
                break
        else:
            return {
                "error": f"Timeout: OpenHands não concluiu em {timeout_sec}s.",
                "conv_id": conv_id,
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
    """
    Submete uma tarefa ao OpenHands e aguarda o resultado (polling REST).

    Fluxo:
      1. POST /api/conversations  → obtém conversation_id
      2. Polling GET /api/conversations/{id} até status == STOPPED
      3. GET /api/conversations/{id}/trajectory → extrai mensagens do agente

    Retorna dict com chave 'output' (str) ou 'error' (str).
    """
    if not _is_available():
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
