import "dotenv/config";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { Telegraf } from "telegraf";

const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const ALLOWED_USER_ID = Number(process.env.ALLOWED_TELEGRAM_USER_ID || "0");
const SHARED_SECRET = process.env.BOT_SHARED_SECRET || "";
const DATA_DIR = process.env.DATA_DIR || path.resolve(process.cwd(), "../../data");
const LOG_PATH = path.join(DATA_DIR, "audit.log");

if (!BOT_TOKEN) {
  console.error("Missing TELEGRAM_BOT_TOKEN");
  process.exit(1);
}

fs.mkdirSync(DATA_DIR, { recursive: true });

function audit(line) {
  const ts = new Date().toISOString();
  fs.appendFileSync(LOG_PATH, `[${ts}] ${line}\n`, "utf8");
}

function isAllowed(ctx) {
  const fromId = ctx.from?.id;
  return fromId && fromId === ALLOWED_USER_ID;
}

function redactSecrets(s) {
  if (!s) return s;
  // very basic redaction
  return s.replaceAll(SHARED_SECRET, "***");
}

function getStatus() {
  const cpus = os.cpus();
  return {
    host: os.hostname(),
    platform: `${os.platform()} ${os.release()}`,
    arch: os.arch(),
    uptime_sec: Math.floor(os.uptime()),
    loadavg: os.loadavg(),
    cpus: cpus?.length || 0,
    totalmem_mb: Math.round(os.totalmem() / 1024 / 1024),
    freemem_mb: Math.round(os.freemem() / 1024 / 1024)
  };
}

function runCommand(command) {
  return new Promise((resolve) => {
    const isWin = process.platform === "win32";
    const shell = isWin ? "powershell.exe" : "bash";
    const args = isWin
      ? ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
      : ["-lc", command];

    const child = spawn(shell, args, { stdio: ["ignore", "pipe", "pipe"] });

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr.on("data", (d) => (stderr += d.toString()));

    child.on("close", (code) => {
      resolve({ code, stdout, stderr });
    });
  });
}

const bot = new Telegraf(BOT_TOKEN);

bot.use(async (ctx, next) => {
  if (!isAllowed(ctx)) {
    if (ctx.message?.text) {
      audit(`DENY user=${ctx.from?.id} text=${ctx.message.text}`);
    }
    return; // ignore silently
  }
  return next();
});

bot.start((ctx) => ctx.reply("OK. Use /status or /exec <secret> <command>"));

bot.command("status", async (ctx) => {
  const st = getStatus();
  audit(`STATUS user=${ctx.from.id}`);
  await ctx.reply("```json\n" + JSON.stringify(st, null, 2) + "\n```", { parse_mode: "Markdown" });
});

bot.command("exec", async (ctx) => {
  const text = ctx.message.text || "";
  // expected: /exec <secret> <command...>
  const parts = text.split(" ").slice(1);
  const secret = parts.shift() || "";
  const cmd = parts.join(" ").trim();

  if (!cmd) return ctx.reply("Usage: /exec <secret> <command>");

  if (SHARED_SECRET && secret !== SHARED_SECRET) {
    audit(`EXEC_DENY_BAD_SECRET user=${ctx.from.id}`);
    return ctx.reply("Denied.");
  }

  audit(`EXEC user=${ctx.from.id} cmd=${redactSecrets(cmd)}`);

  const res = await runCommand(cmd);

  const out = [
    `exit_code: ${res.code}`,
    res.stdout ? `stdout:\n${res.stdout}` : "",
    res.stderr ? `stderr:\n${res.stderr}` : ""
  ]
    .filter(Boolean)
    .join("\n\n")
    .slice(0, 3500);

  await ctx.reply("```text\n" + out + "\n```", { parse_mode: "Markdown" });
});

bot.launch();
audit("BOT_START");

process.once("SIGINT", () => bot.stop("SIGINT"));
process.once("SIGTERM", () => bot.stop("SIGTERM"));