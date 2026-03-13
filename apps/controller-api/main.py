"""
my-agent — Controller API
Responsabilidades: política de segurança, persistência de tarefas,
controle de aprovação, execução (exec/openhands).
"""

import os
import json
import uuid
import re
import sys
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional
from threading import Lock

import yaml
import requests
from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel

# ── Configuração ──────────────────────────────────────────────────────────────
DATA_DIR = Path(os.getenv("DATA_DIR", "../../data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

TASKS_FILE  = DATA_DIR / "tasks.json"
POLICY_FILE = DATA_DIR / "policy.yaml"
AUDIT_LOG   = DATA_DIR / "audit.log"

CONTROLLER_API_KEY = os.getenv("CONTROLLER_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")   # usado para notificar resultado do OpenHands

_lock = Lock()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _audit(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with _lock:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] CTRL {msg}\n")


def _load_tasks() -> dict:
    if not TASKS_FILE.exists():
        return {}
    with _lock:
        return json.loads(TASKS_FILE.read_text("utf-8"))


def _save_tasks(tasks: dict) -> None:
    with _lock:
        TASKS_FILE.write_text(
            json.dumps(tasks, indent=2, ensure_ascii=False), "utf-8"
        )


def _default_policy() -> dict:
    return {
        "exec": {
            "require_approval": True,
            "max_output_chars": 3500,
            "timeout_sec": 60,
            "blocklist_patterns": [
                r"rm\s+-rf",
                r"del\s+/[fsq]",
                r"format\s+",
                r"\bshutdown\b",
                r"\breboot\b",
                r"Remove-Item.*-Recurse.*-Force",
                r"Stop-Computer",
                r"Restart-Computer",
                r"curl[^|]*\|[^|]*sh",
                r"wget[^|]*\|[^|]*sh",
                r"Invoke-Expression",
                r"iex\s",
            ],
        },
        "openhands": {
            "require_approval": True,
            "max_output_chars": 3500,
            "timeout_sec": 300,
        },
    }


def _load_policy() -> dict:
    defaults = _default_policy()
    if not POLICY_FILE.exists():
        # Persiste padrão para o usuário editar
        with _lock:
            POLICY_FILE.write_text(
                yaml.dump(defaults, allow_unicode=True, default_flow_style=False),
                "utf-8",
            )
        return defaults
    try:
        loaded = yaml.safe_load(POLICY_FILE.read_text("utf-8")) or {}
        for k, v in defaults.items():
            loaded.setdefault(k, v)
        return loaded
    except Exception:
        return defaults


def _check_policy(task_type: str, command: str) -> tuple[bool, str]:
    policy = _load_policy()
    cfg = policy.get(task_type, {})
    for pat in cfg.get("blocklist_patterns", []):
        if re.search(pat, command, re.IGNORECASE):
            return False, f"Bloqueado pelo padrão: {pat}"
    return True, "ok"


def _run_command(command: str, timeout_sec: int = 60, max_chars: int = 3500) -> dict:
    is_win = sys.platform == "win32"
    shell_cmd = (
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
        if is_win
        else ["bash", "-lc", command]
    )
    try:
        result = subprocess.run(
            shell_cmd, capture_output=True, text=True, timeout=timeout_sec
        )
        return {
            "code": result.returncode,
            "stdout": result.stdout[:max_chars],
            "stderr": result.stderr[:max_chars],
        }
    except subprocess.TimeoutExpired:
        return {"code": -1, "stdout": "", "stderr": f"Timeout: comando excedeu {timeout_sec}s"}
    except Exception as exc:
        return {"code": -1, "stdout": "", "stderr": str(exc)}


def _format_exec_output(res: dict, max_chars: int = 3500) -> str:
    parts = [f"exit_code: {res['code']}"]
    if res["stdout"]:
        parts.append(f"stdout:\n{res['stdout']}")
    if res["stderr"]:
        parts.append(f"stderr:\n{res['stderr']}")
    return "\n\n".join(parts)[:max_chars]


def _tg_send(chat_id: int, text: str) -> None:
    """Envia mensagem no Telegram (usado para notificação assíncrona do OpenHands)."""
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass  # Falha silenciosa; o resultado já está salvo em tasks.json


def _execute_task_sync(task_id: str) -> dict:
    """Executa a tarefa e atualiza o estado. Retorna a tarefa atualizada."""
    tasks = _load_tasks()
    task = tasks[task_id]
    policy = _load_policy()
    cfg = policy.get(task["type"], {})
    max_chars = int(cfg.get("max_output_chars", 3500))
    timeout_sec = int(cfg.get("timeout_sec", 60))

    task["status"] = "running"
    tasks[task_id] = task
    _save_tasks(tasks)

    if task["type"] == "exec":
        res = _run_command(task["command"], timeout_sec=timeout_sec, max_chars=max_chars)
        task["result"] = _format_exec_output(res, max_chars)
        task["status"] = "done" if res["code"] == 0 else "failed"
    elif task["type"] == "openhands":
        from openhands_client import run_task_sync
        result = run_task_sync(task["command"], timeout_sec=timeout_sec)
        task["result"] = str(result)[:max_chars]
        task["status"] = "done" if "error" not in result else "failed"

    tasks = _load_tasks()
    tasks[task_id] = task
    _save_tasks(tasks)
    _audit(f"TASK_DONE id={task_id} status={task['status']}")
    return task


def _execute_task_background(task_id: str) -> None:
    """Executa em thread background e notifica via Telegram quando concluído."""
    try:
        task = _execute_task_sync(task_id)
        icon = "✅" if task["status"] == "done" else "❌"
        result_text = task.get("result", "")
        msg = f"{icon} Task `{task_id[:8]}` — {task['status']}\n```\n{result_text[:3000]}\n```"
        _tg_send(task["chat_id"], msg)
    except Exception as exc:
        _audit(f"TASK_BG_ERROR id={task_id} err={exc}")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="my-agent controller", version="0.2.0")


# ── Auth ──────────────────────────────────────────────────────────────────────
def check_api_key(x_api_key: str = Header(default="")) -> None:
    if CONTROLLER_API_KEY and x_api_key != CONTROLLER_API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")


Protected = Depends(check_api_key)


# ── Modelos ───────────────────────────────────────────────────────────────────
class TaskCreate(BaseModel):
    user_id: int
    chat_id: int
    type: Literal["exec", "openhands"]
    command: Optional[str] = None


# ── Rotas ─────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.post("/tasks", status_code=201, dependencies=[Protected])
def create_task(body: TaskCreate):
    command = (body.command or "").strip()
    if not command:
        raise HTTPException(status_code=422, detail="Campo 'command' é obrigatório")

    allowed, reason = _check_policy(body.type, command)
    if not allowed:
        _audit(f"POLICY_DENY user={body.user_id} type={body.type} reason={reason}")
        raise HTTPException(status_code=422, detail={"reason": reason})

    policy = _load_policy()
    require_approval = bool(policy.get(body.type, {}).get("require_approval", True))

    task_id = str(uuid.uuid4())
    task = {
        "id": task_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "user_id": body.user_id,
        "chat_id": body.chat_id,
        "type": body.type,
        "command": command,
        "status": "pending" if require_approval else "approved",
        "result": None,
    }

    tasks = _load_tasks()
    tasks[task_id] = task
    _save_tasks(tasks)
    _audit(f"TASK_CREATED id={task_id} user={body.user_id} type={body.type} require_approval={require_approval}")

    if not require_approval:
        if body.type == "openhands":
            threading.Thread(target=_execute_task_background, args=(task_id,), daemon=True).start()
            tasks = _load_tasks()
            return tasks[task_id]
        task = _execute_task_sync(task_id)

    return task


@app.get("/tasks", dependencies=[Protected])
def list_tasks(limit: int = 20):
    tasks = _load_tasks()
    items = sorted(tasks.values(), key=lambda t: t["created_at"], reverse=True)
    return items[:limit]


@app.get("/tasks/{task_id}", dependencies=[Protected])
def get_task(task_id: str):
    tasks = _load_tasks()
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    return tasks[task_id]


@app.post("/tasks/{task_id}/approve", dependencies=[Protected])
def approve_task(task_id: str):
    tasks = _load_tasks()
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")

    task = tasks[task_id]
    if task["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Tarefa está '{task['status']}', não 'pending'")

    _audit(f"TASK_APPROVED id={task_id}")

    if task["type"] == "openhands":
        task["status"] = "approved"
        tasks[task_id] = task
        _save_tasks(tasks)
        threading.Thread(target=_execute_task_background, args=(task_id,), daemon=True).start()
        return tasks[task_id]

    return _execute_task_sync(task_id)


@app.post("/tasks/{task_id}/reject", dependencies=[Protected])
def reject_task(task_id: str):
    tasks = _load_tasks()
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")

    task = tasks[task_id]
    if task["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Tarefa está '{task['status']}', não 'pending'")

    task["status"] = "rejected"
    tasks[task_id] = task
    _save_tasks(tasks)
    _audit(f"TASK_REJECTED id={task_id}")
    return task
