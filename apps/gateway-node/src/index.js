import "dotenv/config";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Telegraf, Markup } from "telegraf";

const BOT_TOKEN        = process.env.TELEGRAM_BOT_TOKEN;
const ALLOWED_USER_ID  = Number(process.env.ALLOWED_TELEGRAM_USER_ID || "0");
const SHARED_SECRET    = process.env.BOT_SHARED_SECRET || "";
const CONTROLLER_URL   = (process.env.CONTROLLER_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const CONTROLLER_KEY   = process.env.CONTROLLER_API_KEY || "";
const DATA_DIR         = process.env.DATA_DIR || path.resolve(process.cwd(), "../../data");
const LOG_PATH         = path.join(DATA_DIR, "audit.log");

if (!BOT_TOKEN) {
  console.error("TELEGRAM_BOT_TOKEN ausente no .env");
  process.exit(1);
}

fs.mkdirSync(DATA_DIR, { recursive: true });

// ── Helpers ───────────────────────────────────────────────────────────────────

function audit(line) {
  const ts = new Date().toISOString();
  fs.appendFileSync(LOG_PATH, `[${ts}] BOT ${line}\n`, "utf8");
}

function isAllowed(ctx) {
  return ctx.from?.id === ALLOWED_USER_ID;
}

function getStatus() {
  return {
    host: os.hostname(),
    platform: `${os.platform()} ${os.release()}`,
    arch: os.arch(),
    uptime_sec: Math.floor(os.uptime()),
    cpus: os.cpus()?.length || 0,
    totalmem_mb: Math.round(os.totalmem() / 1024 / 1024),
    freemem_mb: Math.round(os.freemem() / 1024 / 1024),
    controller: CONTROLLER_URL,
  };
}

async function controllerFetch(endpoint, options = {}) {
  const headers = { "Content-Type": "application/json" };
  if (CONTROLLER_KEY) headers["x-api-key"] = CONTROLLER_KEY;
  const res = await fetch(`${CONTROLLER_URL}${endpoint}`, {
    ...options,
    headers: { ...headers, ...(options.headers || {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(`Controller ${res.status}`), { body, status: res.status });
  return body;
}

async function submitTask(userId, chatId, type, command) {
  return controllerFetch("/tasks", {
    method: "POST",
    body: JSON.stringify({ user_id: userId, chat_id: chatId, type, command }),
  });
}

function formatResult(task) {
  const icon = task.status === "done" ? "✅" : task.status === "failed" ? "❌" : "🔄";
  const header = `${icon} Task \`${task.id.slice(0, 8)}\` — *${task.status}*`;
  const body = task.result
    ? "```\n" + task.result.slice(0, 3000) + "\n```"
    : "";
  return [header, body].filter(Boolean).join("\n");
}

// ── Bot ───────────────────────────────────────────────────────────────────────

const bot = new Telegraf(BOT_TOKEN);

// Auth middleware
bot.use(async (ctx, next) => {
  if (!isAllowed(ctx)) {
    if (ctx.message?.text) audit(`DENY user=${ctx.from?.id}`);
    return;
  }
  return next();
});

bot.start((ctx) =>
  ctx.reply(
    "🤖 *my\\-agent* online\n\n" +
    "Comandos:\n" +
    "/status — informações do host\n" +
    "/exec \\<secret\\> \\<comando\\> — executa no host \\(via controller\\)\n" +
    "/think \\<tarefa\\> — envia para o OpenHands\n" +
    "/tasks — últimas tarefas\n" +
    "/providers — provedores LLM disponíveis\n" +
    "/setmodel — selecionar modelo Ollama LOCAL",
    { parse_mode: "MarkdownV2" }
  )
);

bot.command("status", async (ctx) => {
  const st = getStatus();
  audit(`STATUS user=${ctx.from.id}`);
  await ctx.reply("```json\n" + JSON.stringify(st, null, 2) + "\n```", {
    parse_mode: "Markdown",
  });
});

bot.command("exec", async (ctx) => {
  const text = ctx.message.text || "";
  const parts = text.split(" ").slice(1);
  const secret = parts.shift() || "";
  const cmd = parts.join(" ").trim();

  if (!cmd) return ctx.reply("Uso: /exec <secret> <comando>");

  if (SHARED_SECRET && secret !== SHARED_SECRET) {
    audit(`EXEC_DENY_BAD_SECRET user=${ctx.from.id}`);
    return ctx.reply("❌ Negado.");
  }

  audit(`EXEC_SUBMIT user=${ctx.from.id} cmd=${cmd.slice(0, 80)}`);

  let task;
  try {
    task = await submitTask(ctx.from.id, ctx.chat.id, "exec", cmd);
  } catch (err) {
    if (err.status === 422) {
      const reason = err.body?.detail?.reason || err.body?.detail || "política de segurança";
      return ctx.reply(`🚫 Bloqueado: ${reason}`);
    }
    if (err.message.includes("fetch failed") || err.message.includes("ECONNREFUSED")) {
      return ctx.reply("⚠️ Controller offline. Inicie com:\n```\ncd apps/controller-api\nuvicorn main:app\n```", { parse_mode: "Markdown" });
    }
    return ctx.reply(`⚠️ Erro: ${err.message.slice(0, 200)}`);
  }

  if (task.status === "pending") {
    await ctx.reply(
      `⏳ Aguarda aprovação:\n\`${cmd.slice(0, 200)}\`\n\nTask: \`${task.id.slice(0, 8)}\``,
      {
        parse_mode: "Markdown",
        ...Markup.inlineKeyboard([
          Markup.button.callback("✅ Aprovar", `approve:${task.id}`),
          Markup.button.callback("❌ Rejeitar", `reject:${task.id}`),
        ]),
      }
    );
  } else {
    await ctx.reply(formatResult(task), { parse_mode: "Markdown" });
  }
});

bot.command("think", async (ctx) => {
  const prompt = ctx.message.text.replace(/^\/think\s*/i, "").trim();
  if (!prompt) return ctx.reply("Uso: /think <tarefa ou pergunta para o agente>");

  audit(`THINK_SUBMIT user=${ctx.from.id} prompt=${prompt.slice(0, 80)}`);

  let task;
  try {
    task = await submitTask(ctx.from.id, ctx.chat.id, "openhands", prompt);
  } catch (err) {
    if (err.message.includes("fetch failed") || err.message.includes("ECONNREFUSED")) {
      return ctx.reply("⚠️ Controller offline.");
    }
    return ctx.reply(`⚠️ Erro: ${err.message.slice(0, 200)}`);
  }

  if (task.status === "pending") {
    await ctx.reply(
      `🧠 Tarefa para OpenHands:\n_${prompt.slice(0, 300)}_\n\nTask: \`${task.id.slice(0, 8)}\``,
      {
        parse_mode: "Markdown",
        ...Markup.inlineKeyboard([
          Markup.button.callback("✅ Aprovar", `approve:${task.id}`),
          Markup.button.callback("❌ Rejeitar", `reject:${task.id}`),
        ]),
      }
    );
  } else if (task.status === "approved" || task.status === "running") {
    await ctx.reply(`🔄 Task \`${task.id.slice(0, 8)}\` em execução no OpenHands. Você será notificado quando concluir.`, { parse_mode: "Markdown" });
  } else {
    await ctx.reply(formatResult(task), { parse_mode: "Markdown" });
  }
});

bot.command("tasks", async (ctx) => {
  let tasks;
  try {
    tasks = await controllerFetch("/tasks?limit=5");
  } catch (err) {
    return ctx.reply("⚠️ Controller offline ou erro ao buscar tarefas.");
  }
  if (!tasks.length) return ctx.reply("Nenhuma tarefa ainda.");
  const lines = tasks.map(
    (t) => `• \`${t.id.slice(0, 8)}\` [${t.status}] ${t.type}: ${(t.command || "").slice(0, 60)}`
  );
  await ctx.reply(lines.join("\n"), { parse_mode: "Markdown" });
});

bot.command("providers", async (ctx) => {
  let data;
  try {
    data = await controllerFetch("/providers");
  } catch (err) {
    return ctx.reply("\u26a0\ufe0f Erro ao consultar provedores: " + err.message.slice(0, 200));
  }

  const active = data.active || {};
  const ollama = data.ollama || {};

  const icon = active.provider === "ollama" ? "\uD83D\uDCBB" : "\u2601\ufe0f";
  let msg = `${icon} *Provedor ativo:* \`${active.model}\`\n`;
  if (active.base_url && active.provider === "ollama") {
    msg += `\u2514 URL: \`${active.base_url}\`\n`;
  }
  msg += "\n";

  if (ollama.available) {
    msg += `*Ollama LOCAL* — ${ollama.models.length} modelo(s):\n`;
    msg += ollama.models.map((m) => {
      const name = m.name || m;
      const isActive = active.model === `ollama/${name}` || active.model === name;
      return `  ${isActive ? "✅" : "•"} \`${name}\``;
    }).join("\n");
    msg += "\n\nUse /setmodel para trocar o modelo ativo.";
  } else {
    msg += "*Ollama LOCAL* — nenhum modelo LLM instalado.\n";
    msg += "Execute para baixar:\n```\nollama pull llama3\n```";
  }

  await ctx.reply(msg, { parse_mode: "Markdown" });
});

bot.command("setmodel", async (ctx) => {
  let data;
  try {
    data = await controllerFetch("/providers");
  } catch (err) {
    return ctx.reply("⚠️ Controller offline.");
  }

  const ollama = data.ollama || {};
  const active = data.active || {};

  if (!ollama.available || !ollama.models.length) {
    return ctx.reply(
      "❌ Nenhum modelo Ollama LOCAL instalado.\n\nInstale com:\n```\nollama pull llama3\n```",
      { parse_mode: "Markdown" }
    );
  }

  // Monta teclado: 1 botão por linha, marca o modelo ativo com ✅
  const buttons = ollama.models.map((m) => {
    const isActive = active.model === `ollama/${m.name}` || active.model === m.name;
    const label = isActive ? `✅ ${m.name}` : m.name;
    return [Markup.button.callback(label, `setmodel:${m.name}`)];
  });

  await ctx.reply(
    `*Selecione o modelo Ollama LOCAL:*\n\nAtivo: \`${active.model}\`\n_Sem necessidade de API Key_`,
    {
      parse_mode: "Markdown",
      ...Markup.inlineKeyboard(buttons),
    }
  );
});

// ── Callbacks teclado inline ──────────────────────────────────────────────────

bot.action(/^setmodel:(.+)$/, async (ctx) => {
  const modelName = ctx.match[1];
  await ctx.answerCbQuery("\u23f3 Configurando...");
  try {
    const result = await controllerFetch("/settings/llm", {
      method: "POST",
      body: JSON.stringify({
        provider: "ollama",
        model: modelName,
      }),
    });
    const syncIcon = result.openhands_sync === "ok" ? "\u2705" : "\u26a0\ufe0f";
    await ctx.editMessageText(
      `\uD83D\uDCBB *Modelo alterado!*\n\n` +
      `Modelo: \`${result.model}\`\n` +
      `${syncIcon} OpenHands: ${result.openhands_sync}\n\n` +
      `_Sem API Key \u2014 rodando 100% local_`,
      { parse_mode: "Markdown" }
    );
  } catch (err) {
    await ctx.editMessageText("\u26a0\ufe0f Erro ao alterar modelo: " + err.message.slice(0, 200));
  }
});

bot.action(/^approve:(.+)$/, async (ctx) => {
  const taskId = ctx.match[1];
  await ctx.answerCbQuery("⏳ Aprovando...");
  try {
    const task = await controllerFetch(`/tasks/${taskId}/approve`, { method: "POST" });
    if (task.status === "approved" || task.status === "running") {
      await ctx.editMessageText(
        `🔄 Task \`${taskId.slice(0, 8)}\` aprovada — executando em background.\nVocê será notificado quando concluir.`,
        { parse_mode: "Markdown" }
      );
    } else {
      await ctx.editMessageText(formatResult(task), { parse_mode: "Markdown" });
    }
  } catch (err) {
    await ctx.editMessageText(`⚠️ Erro ao aprovar: ${err.message.slice(0, 200)}`);
  }
});

bot.action(/^reject:(.+)$/, async (ctx) => {
  const taskId = ctx.match[1];
  await ctx.answerCbQuery("Rejeitado.");
  try {
    const task = await controllerFetch(`/tasks/${taskId}/reject`, { method: "POST" });
    await ctx.editMessageText(`🚫 Task \`${task.id.slice(0, 8)}\` rejeitada.`, {
      parse_mode: "Markdown",
    });
  } catch (err) {
    await ctx.editMessageText(`⚠️ Erro ao rejeitar: ${err.message.slice(0, 200)}`);
  }
});

bot.launch();
audit("BOT_START");

process.once("SIGINT", () => bot.stop("SIGINT"));
process.once("SIGTERM", () => bot.stop("SIGTERM"));