"""
Cliente OpenHands para o Controller API.

Documentação: https://github.com/All-Hands-AI/OpenHands
Inicia o servidor com: vendor/openhands/docker-compose.yml

Este módulo será complementado quando OpenHands estiver rodando localmente.
"""

import os
import time
import requests

OPENHANDS_URL = os.getenv("OPENHANDS_URL", "http://127.0.0.1:3000").rstrip("/")


def run_task_sync(prompt: str, timeout_sec: int = 300) -> dict:
    """
    Submete uma tarefa ao OpenHands e aguarda o resultado (polling síncrono).
    Retorna dict com chave 'output' ou 'error'.
    """
    if not OPENHANDS_URL:
        return {"error": "OPENHANDS_URL não configurado. Inicie com vendor/openhands/docker-compose.yml"}

    try:
        # 1. Verifica se o OpenHands está disponível
        health = requests.get(f"{OPENHANDS_URL}/api/options/models", timeout=5)
        if not health.ok:
            return {"error": f"OpenHands não respondeu ({health.status_code}). Verifique se está rodando."}
    except requests.exceptions.ConnectionError:
        return {"error": f"OpenHands inacessível em {OPENHANDS_URL}. Inicie com: docker compose up -d (em vendor/openhands/)"}

    try:
        # 2. Cria uma conversa
        resp = requests.post(
            f"{OPENHANDS_URL}/api/conversations",
            json={"initial_user_message": prompt},
            timeout=30,
        )
        resp.raise_for_status()
        conv = resp.json()
        conv_id = conv.get("conversation_id") or conv.get("id")
        if not conv_id:
            return {"error": f"Resposta inesperada do OpenHands: {conv}"}

        # 3. Polling até conclusão
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            time.sleep(3)
            status_resp = requests.get(
                f"{OPENHANDS_URL}/api/conversations/{conv_id}", timeout=10
            )
            if not status_resp.ok:
                continue
            data = status_resp.json()
            state = data.get("status") or data.get("state", "")
            if state in ("finished", "error", "stopped", "complete"):
                # Extrai mensagens do agente como output
                messages = data.get("messages") or []
                agent_msgs = [m.get("content", "") for m in messages if m.get("role") == "assistant"]
                output = "\n\n".join(agent_msgs) if agent_msgs else str(data)
                return {"output": output, "state": state, "conv_id": conv_id}

        return {"error": f"Timeout: OpenHands não concluiu em {timeout_sec}s. conv_id={conv_id}"}

    except Exception as exc:
        return {"error": str(exc)}
