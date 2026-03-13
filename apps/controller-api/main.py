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
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

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
        state  = result.get("state", "")
        if "error" in result:
            # Mensagem amigável para rate limit
            if state in ("RATE_LIMITED", "ERROR"):
                task["result"] = result["error"]
                if result.get("output"):
                    task["result"] += f"\n\nOutput parcial:\n{result['output']}"
            else:
                task["result"] = result["error"]
            task["status"] = "failed"
        else:
            task["result"] = str(result.get("output", ""))[:max_chars]
            task["status"] = "done"

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


@app.get("/ui", response_class=HTMLResponse)
def settings_ui():
    """Página de configuração do LLM — lista modelos Ollama disponíveis localmente."""
    api_key = CONTROLLER_API_KEY or ""
    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>my-agent — Configurações LLM</title>
<style>
  :root {{
    --bg: #0f1117; --panel: #1a1d27; --border: #2d3148;
    --accent: #6e56cf; --accent2: #3d9eff; --text: #e2e4f0;
    --muted: #8b8fa8; --green: #22c55e; --red: #ef4444; --yellow: #f59e0b;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; min-height: 100vh; display: flex; align-items: flex-start; justify-content: center; padding: 2rem 1rem; }}
  .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 2rem; width: 100%; max-width: 560px; }}
  h1 {{ font-size: 1.3rem; font-weight: 700; margin-bottom: 0.3rem; display: flex; align-items: center; gap: 0.5rem; }}
  .subtitle {{ color: var(--muted); font-size: 0.85rem; margin-bottom: 2rem; }}
  label {{ display: block; font-size: 0.8rem; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.5rem; margin-top: 1.2rem; }}
  select, input {{ width: 100%; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; color: var(--text); padding: 0.65rem 0.85rem; font-size: 0.95rem; outline: none; transition: border-color 0.15s; }}
  select:focus, input:focus {{ border-color: var(--accent); }}
  select option {{ background: var(--panel); }}
  .provider-tabs {{ display: flex; gap: 0.75rem; margin-top: 0.5rem; }}
  .tab {{ flex: 1; padding: 0.75rem; border: 2px solid var(--border); border-radius: 10px; cursor: pointer; text-align: center; font-size: 0.9rem; font-weight: 600; transition: all 0.15s; background: var(--bg); color: var(--muted); }}
  .tab:hover {{ border-color: var(--accent); color: var(--text); }}
  .tab.active {{ border-color: var(--accent); background: rgba(110,86,207,0.15); color: var(--text); }}
  .tab .icon {{ font-size: 1.4rem; display: block; margin-bottom: 0.25rem; }}
  .tab .badge {{ display: inline-block; background: var(--green); color: #000; font-size: 0.65rem; font-weight: 700; border-radius: 4px; padding: 1px 5px; margin-left: 4px; vertical-align: middle; }}
  .section {{ margin-top: 1rem; }}
  .section.hidden {{ display: none; }}
  .model-grid {{ display: flex; flex-direction: column; gap: 0.5rem; margin-top: 0.5rem; }}
  .model-btn {{ display: flex; align-items: center; gap: 0.75rem; background: var(--bg); border: 2px solid var(--border); border-radius: 8px; padding: 0.75rem 1rem; cursor: pointer; transition: all 0.15s; text-align: left; color: var(--text); font-size: 0.9rem; }}
  .model-btn:hover {{ border-color: var(--accent2); }}
  .model-btn.selected {{ border-color: var(--green); background: rgba(34,197,94,0.08); }}
  .model-btn .mname {{ font-weight: 600; }}
  .model-btn .msize {{ color: var(--muted); font-size: 0.78rem; }}
  .model-btn .check {{ margin-left: auto; color: var(--green); font-size: 1.1rem; opacity: 0; }}
  .model-btn.selected .check {{ opacity: 1; }}
  .no-models {{ color: var(--yellow); background: rgba(245,158,11,0.1); border: 1px solid rgba(245,158,11,0.3); border-radius: 8px; padding: 1rem; font-size: 0.88rem; line-height: 1.6; }}
  .no-models code {{ background: rgba(255,255,255,0.1); padding: 2px 6px; border-radius: 4px; font-family: monospace; }}
  .apply-btn {{ width: 100%; margin-top: 1.5rem; padding: 0.85rem; background: var(--accent); border: none; border-radius: 8px; color: #fff; font-size: 1rem; font-weight: 700; cursor: pointer; transition: opacity 0.15s; }}
  .apply-btn:hover {{ opacity: 0.85; }}
  .apply-btn:disabled {{ opacity: 0.4; cursor: not-allowed; }}
  .status {{ margin-top: 1rem; padding: 0.85rem 1rem; border-radius: 8px; font-size: 0.88rem; display: none; }}
  .status.ok {{ background: rgba(34,197,94,0.12); border: 1px solid var(--green); color: var(--green); display: block; }}
  .status.err {{ background: rgba(239,68,68,0.12); border: 1px solid var(--red); color: var(--red); display: block; }}
  .active-badge {{ display: inline-flex; align-items: center; gap: 0.4rem; background: rgba(61,158,255,0.12); border: 1px solid rgba(61,158,255,0.4); color: var(--accent2); border-radius: 6px; padding: 0.4rem 0.75rem; font-size: 0.82rem; margin-bottom: 1.5rem; }}
  .spin {{ display: inline-block; animation: spin 1s linear infinite; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  .adv-section {{ border-top: 1px solid var(--border); margin-top: 1.5rem; padding-top: 1.2rem; }}
  .adv-toggle {{ color: var(--muted); font-size: 0.82rem; cursor: pointer; user-select: none; }}
  .adv-toggle:hover {{ color: var(--text); }}
  .cloud-note {{ color: var(--muted); font-size: 0.82rem; line-height: 1.6; margin-top: 0.5rem; background: rgba(255,255,255,0.03); border-radius: 6px; padding: 0.75rem; }}
</style>
</head>
<body>
<div class="card">
  <h1>⚙️ my-agent — LLM</h1>
  <p class="subtitle">Configurações do modelo de linguagem para o OpenHands</p>

  <div id="activeBadge" class="active-badge"><span class="spin">⏳</span> Carregando...</div>

  <label>Provedor</label>
  <div class="provider-tabs">
    <div class="tab active" id="tab-ollama" onclick="selectProvider('ollama')">
      <span class="icon">🏠</span>
      Ollama LOCAL
      <span class="badge" id="local-count" style="display:none">0</span>
    </div>
    <div class="tab" id="tab-groq" onclick="selectProvider('groq')">
      <span class="icon">⚡</span>
      Groq
    </div>
    <div class="tab" id="tab-other" onclick="selectProvider('other')">
      <span class="icon">☁️</span>
      Outro
    </div>
  </div>

  <!-- OLLAMA -->
  <div class="section" id="sec-ollama">
    <label>Modelos instalados localmente</label>
    <div id="model-grid" class="model-grid">
      <div style="color:var(--muted);font-size:0.88rem">⏳ Consultando Ollama...</div>
    </div>
  </div>

  <!-- GROQ -->
  <div class="section hidden" id="sec-groq">
    <div class="cloud-note">
      Groq oferece modelos rápidos na nuvem.<br>
      ⚠️ Plano free: 12.000 tokens/min — pode atingir rate limit em tarefas longas.<br>
      Obtenha sua chave em <a href="https://console.groq.com/keys" target="_blank" style="color:var(--accent2)">console.groq.com</a>
    </div>
    <label>Modelo</label>
    <select id="groq-model">
      <option value="groq/llama-3.3-70b-versatile">llama-3.3-70b-versatile (recomendado)</option>
      <option value="groq/llama-3.1-8b-instant">llama-3.1-8b-instant (mais rápido)</option>
      <option value="groq/mixtral-8x7b-32768">mixtral-8x7b-32768</option>
      <option value="groq/gemma2-9b-it">gemma2-9b-it</option>
    </select>
    <label>API Key</label>
    <input type="password" id="groq-key" placeholder="gsk_..." />
  </div>

  <!-- OUTRO -->
  <div class="section hidden" id="sec-other">
    <label>Modelo (formato litellm)</label>
    <input type="text" id="other-model" placeholder="ex: openai/gpt-4o" />
    <label>Base URL (opcional)</label>
    <input type="text" id="other-url" placeholder="ex: http://localhost:1234/v1" />
    <label>API Key (opcional)</label>
    <input type="password" id="other-key" placeholder="sk-..." />
  </div>

  <button class="apply-btn" id="applyBtn" onclick="applySettings()">Aplicar configuração</button>
  <button class="apply-btn" id="backBtn" onclick="location.reload()" style="display:none;background:var(--accent2)">↩ Configurar outro modelo</button>
  <div class="status" id="status"></div>
</div>

<script>
const API = '';
const HEADERS = {{'Content-Type': 'application/json', 'x-api-key': '{api_key}'}};

let selectedProvider = 'ollama';
let selectedModel = '';
let ollamaModels = [];

async function init() {{
  try {{
    const r = await fetch(API + '/providers', {{headers: HEADERS}});
    const data = await r.json();
    const active = data.active || {{}};
    const ollama = data.ollama || {{}};

    // Badge ativo
    const badge = document.getElementById('activeBadge');
    const icon = active.provider === 'ollama' ? '🏠' : '⚡';
    badge.innerHTML = `${{icon}} Ativo: <strong style="margin-left:4px">${{active.model}}</strong>`;

    // Conta modelos locais
    ollamaModels = ollama.models || [];
    const cnt = document.getElementById('local-count');
    if (ollamaModels.length > 0) {{
      cnt.textContent = ollamaModels.length;
      cnt.style.display = 'inline-block';
    }}

    renderOllamaModels(active.model);

    // Pré-seleciona o provedor ativo
    if (active.provider !== 'ollama') selectProvider(active.provider === 'groq' ? 'groq' : 'other');
  }} catch(e) {{
    document.getElementById('activeBadge').innerHTML = '⚠️ Controller offline';
  }}
}}

function renderOllamaModels(activeModel) {{
  const grid = document.getElementById('model-grid');
  if (!ollamaModels.length) {{
    grid.innerHTML = `<div class="no-models">
      ⚠️ Nenhum modelo LLM local instalado.<br><br>
      Para baixar um modelo abra um terminal e execute:<br>
      <code>ollama pull llama3</code><br>
      <code>ollama pull mistral</code><br>
      <code>ollama pull phi3</code><br><br>
      Depois recarregue esta página.
    </div>`;
    return;
  }}
  grid.innerHTML = ollamaModels.map(m => {{
    const name = m.name || m;
    const isActive = activeModel === 'ollama/' + name || activeModel === name;
    if (isActive && !selectedModel) selectedModel = name;
    return `<button class="model-btn ${{isActive ? 'selected' : ''}}" onclick="selectModel(this, '${{name}}')" id="mbtn-${{name.replace(/[:.]/g,'_')}}">
      <span>🤖</span>
      <span><span class="mname">${{name}}</span></span>
      <span class="check">✓</span>
    </button>`;
  }}).join('');
}}

function selectModel(el, name) {{
  document.querySelectorAll('.model-btn').forEach(b => b.classList.remove('selected'));
  el.classList.add('selected');
  selectedModel = name;
  setStatus('', '');
}}

function selectProvider(p) {{
  selectedProvider = p;
  ['ollama','groq','other'].forEach(id => {{
    document.getElementById('tab-' + id).classList.toggle('active', id === p);
    document.getElementById('sec-' + id).classList.toggle('hidden', id !== p);
  }});
}}

function setStatus(msg, type) {{
  const el = document.getElementById('status');
  el.textContent = msg;
  el.className = 'status' + (type ? ' ' + type : '');
}}

async function applySettings() {{
  const btn = document.getElementById('applyBtn');
  btn.disabled = true;
  btn.textContent = '⏳ Aplicando...';
  setStatus('', '');

  let body;
  if (selectedProvider === 'ollama') {{
    if (!selectedModel) {{ setStatus('Selecione um modelo primeiro.', 'err'); btn.disabled=false; btn.textContent='Aplicar configuração'; return; }}
    body = {{provider: 'ollama', model: selectedModel}};
  }} else if (selectedProvider === 'groq') {{
    const key = document.getElementById('groq-key').value.trim();
    const model = document.getElementById('groq-model').value;
    if (!key) {{ setStatus('Informe a API Key do Groq.', 'err'); btn.disabled=false; btn.textContent='Aplicar configuração'; return; }}
    body = {{provider: 'groq', model, api_key: key}};
  }} else {{
    body = {{
      provider: 'other',
      model: document.getElementById('other-model').value.trim(),
      api_key: document.getElementById('other-key').value.trim(),
      base_url: document.getElementById('other-url').value.trim(),
    }};
    if (!body.model) {{ setStatus('Informe o nome do modelo.', 'err'); btn.disabled=false; btn.textContent='Aplicar configuração'; return; }}
  }}

  try {{
    const r = await fetch(API + '/settings/llm', {{method: 'POST', headers: HEADERS, body: JSON.stringify(body)}});
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || JSON.stringify(data));
    const sync = data.openhands_sync === 'ok' ? '✅ OpenHands sincronizado' : '⚠️ ' + data.openhands_sync;
    setStatus(`✅ Modelo alterado para ${{data.model}} — ${{sync}}`, 'ok');
    // Atualiza badge
    document.getElementById('activeBadge').innerHTML = `🏠 Ativo: <strong style="margin-left:4px">${{data.model}}</strong>`;
    // Mostra botão "Configurar outro modelo"
    document.getElementById('backBtn').style.display = 'block';
    btn.style.display = 'none';
  }} catch(e) {{
    setStatus('Erro: ' + e.message, 'err');
    btn.disabled = false;
    btn.textContent = 'Aplicar configuração';
    return;
  }}

  btn.disabled = false;
  btn.textContent = 'Aplicar configuração';
}}

init();
</script>
</body>
</html>
""")


@app.get("/health")
def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/providers", dependencies=[Protected])
def list_providers():
    """Lista provedores LLM disponíveis: Ollama local + configuração ativa do OpenHands."""
    from openhands_client import list_ollama_models, _resolve_model, OLLAMA_HOST, load_llm_config
    model, _, base_url = _resolve_model()
    ollama_models = list_ollama_models()
    cfg = load_llm_config()
    return {
        "active": {
            "model": model,
            "base_url": base_url or "(padrão do provedor)",
            "provider": "ollama" if model.startswith("ollama/") else "cloud",
            "source": "runtime" if cfg else "env",
        },
        "ollama": {
            "available": bool(ollama_models),
            "host": OLLAMA_HOST,
            "models": [{"name": m, "label": m} for m in ollama_models],
            "hint": (
                "Nenhum modelo LLM instalado. Execute:\n"
                "  ollama pull llama3\n"
                "  ollama pull mistral"
            ) if not ollama_models else None,
        },
    }


class LlmSettings(BaseModel):
    provider: str          # "ollama" | "groq" | "openai" | etc
    model: str             # ex: "llama3:latest"  ou  "groq/llama-3.3-70b-versatile"
    api_key: Optional[str] = None
    base_url: Optional[str] = None


@app.post("/settings/llm", dependencies=[Protected])
def update_llm_settings(body: LlmSettings):
    """
    Altera o LLM ativo em tempo real — persiste no DATA_DIR/llm_config.json
    e reconfigura o OpenHands imediatamente.
    """
    from openhands_client import save_llm_config, _ensure_settings, _is_available, OLLAMA_HOST

    # Normaliza campos conforme provedor
    if body.provider == "ollama":
        model    = f"ollama/{body.model}" if not body.model.startswith("ollama/") else body.model
        api_key  = body.api_key or "ollama"
        base_url = body.base_url or "http://host.docker.internal:11434"
    else:
        model    = body.model
        api_key  = body.api_key or ""
        base_url = body.base_url or ""

    save_llm_config(model, api_key, base_url)
    _audit(f"LLM_CHANGED model={model} provider={body.provider}")

    # Empurra para OpenHands se estiver disponível
    oh_status = "skipped"
    if _is_available():
        err = _ensure_settings()
        oh_status = "ok" if err is None else f"error: {err}"

    return {
        "model": model,
        "base_url": base_url,
        "provider": body.provider,
        "openhands_sync": oh_status,
    }


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
