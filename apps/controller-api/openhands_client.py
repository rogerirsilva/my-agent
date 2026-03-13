"""
Cliente OpenHands para o Controller API.

API verificada na versão 0.28:
  POST   /api/conversations            — cria conversa (campo: initial_user_msg)
  GET    /api/conversations/{id}       — status: RUNNING | STOPPED
  GET    /api/conversations/{id}/trajectory — lista de eventos (output)
  GET    /health                       — health check
  GET    /api/options/models           — lista modelos disponíveis

Inicia o servidor com:
  cd vendor/openhands && docker compose up -d
"""

import os
import time
import requests

OPENHANDS_URL = os.getenv("OPENHANDS_URL", "http://127.0.0.1:3000").rstrip("/")

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


def run_task_sync(prompt: str, timeout_sec: int = 300) -> dict:
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

        # 2. Polling até STOPPED
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            time.sleep(4)
            status_resp = requests.get(
                f"{OPENHANDS_URL}/api/conversations/{conv_id}",
                timeout=10,
            )
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

        # 3. Extrai output da trajectory
        traj_resp = requests.get(
            f"{OPENHANDS_URL}/api/conversations/{conv_id}/trajectory",
            timeout=20,
        )
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
