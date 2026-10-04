/**
 * cloudflare_postman.js — T_Dubber "Moon Mission" Telegram → Kaggle relay
 * ============================================================================
 * A Cloudflare Worker that receives a Telegram webhook update, parses
 *
 *     /dub <video_url> [extra chop_drop args...]
 *
 * and triggers a Kaggle Notebook run by pushing a freshly generated notebook
 * to Kaggle's real execution endpoint:
 *
 *     POST https://www.kaggle.com/api/v1/kernels/push
 *
 * Nothing is written to disk. Both multipart parts are built in memory:
 *
 *     kernel-metadata.json  -> Kaggle kernel manifest (GPU + internet on)
 *     notebook.ipynb        -> minimal valid nbformat-4 notebook whose first
 *                              code cell has <video_url> baked in, a boot cell
 *                              that downloads chop_drop.py from the repo, and a
 *                              launch cell that runs it.
 *
 * ---------------------------------------------------------------------------
 * REQUIRED SECRETS / VARS  (wrangler secret put NAME  |  wrangler kv/vars)
 * ---------------------------------------------------------------------------
 *   KAGGLE_USERNAME        Kaggle account slug  (e.g. engrtarun)
 *   KAGGLE_KEY             Kaggle API key from kaggle.com/settings/api
 *   TELEGRAM_BOT_TOKEN     Bot token from @BotFather
 *
 *   TELEGRAM_SECRET_TOKEN  Recommended. Telegram sends it in the
 *                          X-Telegram-Bot-Api-Secret-Token header; set it with
 *                          setWebhook(..., secret_token=...). Worker rejects
 *                          updates without a matching header.
 *
 * ---------------------------------------------------------------------------
 * OPTIONAL TUNING VARS
 * ---------------------------------------------------------------------------
 *   ALLOWED_CHAT_IDS       Comma list of chat ids allowed to drive the bot.
 *                          Unset = every chat that can talk to the bot.
 *   KAGGLE_PUSH_MODE       auto (default) | multipart | json
 *   KERNEL_ID              Full kernel slug, default `${KAGGLE_USERNAME}/dubber-worker-homura`
 *   KERNEL_SLUG            Slug part only, default `dubber-worker-homura`
 *   KERNEL_TITLE           Kernel title, default derived from the slug
 *   UNIQUE_KERNEL          1 = append a timestamp to the slug (parallel runs,
 *                          avoids colliding with a kernel that is still busy)
 *   CHOP_DROP_URLS         Comma list of download URLs for chop_drop.py
 *                          (defaults to raw.githubusercontent + jsDelivr mirror)
 *                          NOTE: both read from GitHub `main`, so `chop_drop.py`
 *                          must be committed AND pushed, or the Kaggle boot cell
 *                          will refuse to run a file it cannot fetch.
 *   CHOP_DROP_ARGS         Default extra argv for chop_drop.py,
 *                          e.g. "--chunk-minutes 10"
 *   ALLOW_PRIVATE_URLS     1 = allow RFC1918 / link-local video URLs
 *   SEND_KERNEL_LINK       1 = follow the success message with the notebook URL
 *
 * ---------------------------------------------------------------------------
 * DEPLOY
 * ---------------------------------------------------------------------------
 *   npx wrangler deploy cloudflare_postman.js --name t-dubber-postman
 *   npx wrangler secret put KAGGLE_USERNAME
 *   npx wrangler secret put KAGGLE_KEY
 *   npx wrangler secret put TELEGRAM_BOT_TOKEN
 *   npx wrangler secret put TELEGRAM_SECRET_TOKEN
 *
 *   curl "https://api.telegram.org/bot<TOKEN>/setWebhook" \
 *     -d "url=https://t-dubber-postman.<account>.workers.dev/" \
 *     -d "secret_token=<TELEGRAM_SECRET_TOKEN>"
 *
 * ---------------------------------------------------------------------------
 * WHY THE PUSH MODES EXIST (verified against kaggle-api source, not guessed)
 * ---------------------------------------------------------------------------
 * The classic contract for /api/v1/kernels/push is multipart/form-data with
 * two parts: `kernel-metadata.json` + the code file named by `code_file`.
 * The current official kaggle-api client, however, posts a JSON
 * KernelPushRequest/ApiSaveKernelRequest (slug, new_title, text, language,
 * kernel_type, enable_gpu, enable_internet, ...) to the very same URL. Kaggle
 * has shipped both shapes over the years.
 *
 * Default `KAGGLE_PUSH_MODE=auto` therefore sends the multipart push first
 * (the architecture this Worker was built for) and, only if Kaggle rejects it
 * (4xx/5xx, a payload-level `error`, or a content-type complaint), retries the
 * identical kernel once as JSON. Auth failures (401/403/404/429) are treated
 * as fatal and never retried, so bad credentials are never hammered.
 * Set `KAGGLE_PUSH_MODE=multipart` for a strict multipart-only Worker.
 */

"use strict";

const KAGGLE_PUSH_URL = "https://www.kaggle.com/api/v1/kernels/push";
const TELEGRAM_API = "https://api.telegram.org";
const SUCCESS_TEXT = "🚀 Kaggle Moon Mission Pushed Successfully!";
const MAX_URL_LENGTH = 2048;
const MAX_EXTRA_ARGS = 8;
const ARG_TOKEN_RE = /^-{0,2}[A-Za-z0-9][A-Za-z0-9._:=/-]{0,62}$/;
const KERNEL_ID_RE = /^[a-z0-9][a-z0-9-]{0,63}\/[a-z0-9][a-z0-9-]{0,63}$/;
const KAGGLE_TIMEOUT_MS = 25_000;
const TELEGRAM_TIMEOUT_MS = 10_000;

/** Default sources for chop_drop.py: GitHub raw first, jsDelivr as mirror. */
const DEFAULT_CHOP_DROP_URLS = [
  "https://raw.githubusercontent.com/engrtarun/T_Dubber/main/chop_drop.py",
  "https://cdn.jsdelivr.net/gh/engrtarun/T_Dubber@main/chop_drop.py",
];

/** Command surface shown to humans. */
const HELP_TEXT = [
  "🌙 T_Dubber · Moon Mission control",
  "",
  "Usage:",
  "  /dub <video_url>              push a Kaggle GPU run",
  "  /dub <video_url> --chunk-minutes 10   extra chop_drop.py args",
  "",
  "Tip: reply to a message that contains a video link with /dub.",
].join("\n");

/* ========================================================================== *
 * Errors we are happy to show a Telegram user
 * ========================================================================== */

class UserFacingError extends Error {
  constructor(message) {
    super(message);
    this.name = "UserFacingError";
  }
}

class ConfigError extends Error {
  constructor(message) {
    super(message);
    this.name = "ConfigError";
  }
}

/* ========================================================================== *
 * Small utilities
 * ========================================================================== */

function isEnabled(value) {
  return ["1", "true", "yes", "on"].includes(String(value ?? "").trim().toLowerCase());
}

function requireEnv(env, name) {
  const value = (env[name] || "").trim();
  if (!value) throw new ConfigError(`Missing required Worker binding: ${name}`);
  return value;
}

function listEnv(value, fallback = []) {
  const raw = (value || "").trim();
  if (!raw) return fallback;
  return raw
    .split(/[,\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function truncate(text, max = 600) {
  const flat = String(text ?? "").replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}

/** Length-constant-ish comparison so the webhook secret is not brute-forceable. */
function secretsMatch(a, b) {
  const enc = new TextEncoder();
  const left = enc.encode(String(a ?? ""));
  const right = enc.encode(String(b ?? ""));
  const len = Math.max(left.length, right.length, 1);
  let diff = left.length ^ right.length;
  for (let i = 0; i < len; i += 1) {
    const l = left[i % Math.max(left.length, 1)] || 0;
    const r = right[i % Math.max(right.length, 1)] || 0;
    diff |= l ^ r;
  }
  return diff === 0;
}

function basicAuth(env) {
  const user = requireEnv(env, "KAGGLE_USERNAME");
  const key = requireEnv(env, "KAGGLE_KEY");
  // Both are ASCII, so plain btoa() is safe.
  return `Basic ${btoa(`${user}:${key}`)}`;
}

function jsonResponse(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...headers },
  });
}

/* ========================================================================== *
 * URL validation — the video URL lands inside a Python cell, so it is checked
 * before anything is generated, and private/metadata hosts are refused.
 * ========================================================================== */

function isPrivateOrMetadataHost(hostname) {
  const host = String(hostname || "").toLowerCase().replace(/\.$/, "");

  if (
    host === "localhost" ||
    host.endsWith(".localhost") ||
    host.endsWith(".local") ||
    host.endsWith(".localdomain") ||
    host.endsWith(".internal")
  ) {
    return true;
  }

  if (host === "::1" || host === "[::1]" || host === "0.0.0.0") return true;

  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(host)) {
    const [a, b] = host.split(".").map((n) => Number(n));
    if (a > 255 || b > 255) return true; // not a routable address either
    if (a === 0 || a === 10 || a === 127) return true;
    if (a === 192 && b === 168) return true;
    if (a === 172 && b >= 16 && b <= 31) return true;
    if (a === 169 && b === 254) return true; // cloud metadata service
    if (a === 100 && b >= 64 && b <= 127) return true; // CGNAT
  }

  return false;
}

function validateVideoUrl(raw, env) {
  const candidate = String(raw || "").trim();
  if (!candidate) throw new UserFacingError("No video URL found. Usage: /dub <video_url>");
  if (candidate.length > MAX_URL_LENGTH) {
    throw new UserFacingError(`Video URL is too long (max ${MAX_URL_LENGTH} characters).`);
  }
  // The URL is embedded in a Python string literal; JSON.stringify escapes it,
  // but we still refuse control characters, quotes and shell-y metacharacters.
  if (/[\s"'`\\\r\n\t<>{}|^]/.test(candidate)) {
    throw new UserFacingError("Video URL contains unsupported characters.");
  }

  let parsed;
  try {
    parsed = new URL(candidate);
  } catch {
    throw new UserFacingError(`That does not look like a URL: ${truncate(candidate, 120)}`);
  }

  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new UserFacingError("Only http(s) video URLs are supported.");
  }
  if (!isEnabled(env.ALLOW_PRIVATE_URLS) && isPrivateOrMetadataHost(parsed.hostname)) {
    throw new UserFacingError(`Refusing to target a private/metadata host (${parsed.hostname}).`);
  }

  return parsed.href;
}

/* ========================================================================== *
 * Kernel identity — matches kaggle_worker_local/kernel-metadata.json
 * ========================================================================== */

function titleFromSlug(slug) {
  return slug
    .split("-")
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

function resolveKernel(env) {
  const username = requireEnv(env, "KAGGLE_USERNAME").toLowerCase();
  const uniqueSuffix = isEnabled(env.UNIQUE_KERNEL) ? `-${Date.now().toString(36)}` : "";
  const slug = `${(env.KERNEL_SLUG || "dubber-worker-homura").trim().toLowerCase()}${uniqueSuffix}`;
  const id = (env.KERNEL_ID || `${username}/${slug}`).trim().toLowerCase();

  if (!KERNEL_ID_RE.test(id)) {
    throw new ConfigError(`KERNEL_ID must look like owner/slug (lowercase): got "${id}"`);
  }

  const title = (env.KERNEL_TITLE || titleFromSlug(id.split("/")[1])).trim();
  if (title.length < 5) throw new ConfigError("Kernel title must be at least 5 characters.");
  return { id, title };
}

function buildKernelMetadata(env, kernel) {
  return {
    id: kernel.id,
    title: kernel.title,
    code_file: "notebook.ipynb",
    language: "python",
    kernel_type: "notebook",
    is_private: "true",
    enable_gpu: "true",
    enable_internet: "true",
    dataset_sources: [],
    competition_sources: [],
    kernel_sources: [],
  };
}

/* ========================================================================== *
 * Notebook generation (in memory)
 *
 * Notes that matter for Kaggle:
 *  - nbformat 4 / nbformat_minor 5, every cell carries an id.
 *  - `source` is a single string, not an array of lines: the Kaggle client
 *    explicitly joins arrays because "the server expects just one".
 *  - `outputs` is always [] and `execution_count` is null on code cells.
 *  - The video URL reaches Python through JSON.stringify output, which is a
 *    valid Python string literal and can never break out of the cell.
 * ========================================================================== */

function cellId() {
  return crypto.randomUUID().replace(/-/g, "").slice(0, 12);
}

function markdownCell(source) {
  return { id: cellId(), cell_type: "markdown", metadata: {}, source };
}

function codeCell(source) {
  return {
    id: cellId(),
    cell_type: "code",
    metadata: {},
    source,
    outputs: [],
    execution_count: null,
  };
}

function buildNotebook(env, kernel, videoUrl, extraArgs) {
  const chopUrls = listEnv(env.CHOP_DROP_URLS, DEFAULT_CHOP_DROP_URLS);
  const defaultArgs = listEnv(env.CHOP_DROP_ARGS);
  const pushedAt = new Date().toISOString();

  // JSON.stringify doubles as a safe encoder for both the URL and the arg list:
  // its output is a valid Python literal (double-quoted strings, \uXXXX ok).
  const constantsCell = codeCell(
    [
      "# ⚙️ Injected by cloudflare_postman.js — do not edit by hand.",
      "import json, os, subprocess, sys, time, urllib.request",
      "",
      `VIDEO_URL = ${JSON.stringify(videoUrl)}`,
      `CHOP_DROP_URLS = ${JSON.stringify(chopUrls)}`,
      `CHOP_DROP_ARGS = ${JSON.stringify([...defaultArgs, ...extraArgs])}`,
      "",
      'print("[moon-mission] target:", VIDEO_URL, flush=True)',
    ].join("\n")
  );

  const bootCell = codeCell(
    [
      "# ⬇️ Fetch chop_drop.py from the T_Dubber repo (raw first, mirror second).",
      "def _download(urls, dest, tries=4):",
      "    last = None",
      "    for url in urls:",
      "        for attempt in range(1, tries + 1):",
      "            try:",
      "                with urllib.request.urlopen(url, timeout=60) as resp:",
      "                    blob = resp.read()",
      '                if len(blob) < 1024:',
      '                    raise RuntimeError("suspiciously small: %d bytes" % len(blob))',
      '                text = blob.decode("utf-8", "replace")',
      '                if "argparse" not in text or "ffmpeg" not in text:',
      '                    raise RuntimeError("download does not look like chop_drop.py")',
      '                with open(dest, "wb") as fh:',
      "                    fh.write(blob)",
      '                print("[boot] fetched chop_drop.py from %s (%d bytes)" % (url, len(blob)), flush=True)',
      "                return dest",
      "            except Exception as exc:",
      "                last = exc",
      '                print("[boot] attempt %d/%d via %s failed: %s" % (attempt, tries, url, exc), flush=True)',
      "                time.sleep(2 * attempt)",
      '    raise SystemExit("[boot] could not download chop_drop.py from any source: %s" % last)',
      "",
      '_download(CHOP_DROP_URLS, "chop_drop.py")',
    ].join("\n")
  );

  const runCell = codeCell(
    [
      "# 🚀 Moon Mission: stream the video in chunks, dub it, upload it.",
      'cmd = [sys.executable, "chop_drop.py", VIDEO_URL] + CHOP_DROP_ARGS',
      'print("[moon-mission] launching:", " ".join(cmd), flush=True)',
      "result = subprocess.run(cmd)",
      "if result.returncode != 0:",
      '    raise SystemExit("[moon-mission] chop_drop.py failed with exit code %d" % result.returncode)',
      'print("[moon-mission] chop_drop.py finished cleanly", flush=True)',
    ].join("\n")
  );

  return {
    cells: [
      markdownCell(
        [
          `# 🌙 T_Dubber · Moon Mission — \`${kernel.id}\``,
          "",
          `Auto-generated by \`cloudflare_postman.js\` at \`${pushedAt}\`.`,
          "Downloads `chop_drop.py` from the T_Dubber repo and streams the video in 10-minute chunks.",
        ].join("\n")
      ),
      constantsCell,
      bootCell,
      runCell,
    ],
    metadata: {
      kernelspec: { display_name: "Python 3", language: "python", name: "python3" },
      language_info: { name: "python", version: "3.10" },
    },
    nbformat: 4,
    nbformat_minor: 5,
  };
}

/* ========================================================================== *
 * Kaggle transport
 * ========================================================================== */

/**
 * Turn any Kaggle answer into { ok, status, detail, fatal }.
 * Kaggle returns HTTP 200 with {"error": "..."} on payload-level failures,
 * so the body has to be inspected even on a "successful" response.
 */
async function interpretKaggleResponse(response) {
  const raw = await response.text();
  let payload = null;
  try {
    payload = raw ? JSON.parse(raw) : null;
  } catch {
    payload = null;
  }

  const fatalStatuses = new Set([401, 403, 404, 429]);
  const status = response.status;
  const fatal = fatalStatuses.has(status);

  if (status >= 300 && status < 400) {
    const location = response.headers.get("location") || "(no location header)";
    return { ok: false, status, detail: `unexpected redirect to ${truncate(location, 200)}`, fatal };
  }

  if (!response.ok) {
    const detail =
      (payload && (payload.error || payload.message)) || truncate(raw, 500) || `HTTP ${status}`;
    return { ok: false, status, detail: truncate(detail), fatal };
  }

  if (payload && typeof payload.error === "string" && payload.error.trim()) {
    return { ok: false, status, detail: truncate(payload.error), fatal: false };
  }

  return {
    ok: true,
    status,
    detail: truncate((payload && payload.url) || raw || "accepted", 400),
    payload,
    raw,
  };
}

/** Classic shape: multipart/form-data with the two in-memory files. */
async function pushMultipart(env, metadata, notebook) {
  const form = new FormData();
  form.append(
    "kernel-metadata.json",
    new Blob([JSON.stringify(metadata, null, 2)], { type: "application/json" }),
    "kernel-metadata.json"
  );
  form.append(
    "notebook.ipynb",
    new Blob([JSON.stringify(notebook)], { type: "application/json" }),
    "notebook.ipynb"
  );

  // Never set Content-Type manually: fetch() must add its own boundary.
  const response = await fetch(KAGGLE_PUSH_URL, {
    method: "POST",
    headers: { authorization: basicAuth(env) },
    body: form,
    redirect: "manual",
    signal: AbortSignal.timeout(KAGGLE_TIMEOUT_MS),
  });
  return interpretKaggleResponse(response);
}

/** Modern kaggle-api shape: JSON KernelPushRequest carrying the notebook text. */
async function pushJson(env, metadata, notebook) {
  const body = {
    slug: metadata.id,
    new_title: metadata.title,
    text: JSON.stringify(notebook),
    language: metadata.language,
    kernel_type: metadata.kernel_type,
    is_private: true,
    enable_gpu: true,
    enable_tpu: false,
    enable_internet: true,
    dataset_data_sources: metadata.dataset_sources,
    competition_data_sources: metadata.competition_sources,
    kernel_data_sources: metadata.kernel_sources,
    model_data_sources: [],
    category_ids: [],
  };

  const response = await fetch(KAGGLE_PUSH_URL, {
    method: "POST",
    headers: {
      authorization: basicAuth(env),
      "content-type": "application/json",
      accept: "application/json",
    },
    body: JSON.stringify(body),
    redirect: "manual",
    signal: AbortSignal.timeout(KAGGLE_TIMEOUT_MS),
  });
  return interpretKaggleResponse(response);
}

/**
 * Push the kernel, honouring KAGGLE_PUSH_MODE.
 * Returns { ok, mode, status, detail }.
 */
async function pushToKaggle(env, metadata, notebook) {
  const mode = String(env.KAGGLE_PUSH_MODE || "auto").trim().toLowerCase();
  const order =
    mode === "multipart" ? ["multipart"] : mode === "json" ? ["json"] : ["multipart", "json"];

  let last = null;
  for (const attempt of order) {
    try {
      const result = attempt === "multipart"
        ? await pushMultipart(env, metadata, notebook)
        : await pushJson(env, metadata, notebook);

      console.log(
        JSON.stringify({
          event: "kaggle_push",
          mode: attempt,
          ok: result.ok,
          status: result.status,
          kernel: metadata.id,
          detail: result.detail,
        })
      );

      if (result.ok) return { ok: true, mode: attempt, status: result.status, detail: result.detail };
      last = { ok: false, mode: attempt, status: result.status, detail: result.detail };
      if (result.fatal || mode !== "auto") break; // bad creds / rate limit: stop
    } catch (error) {
      last = {
        ok: false,
        mode: attempt,
        status: 0,
        detail: truncate(error && error.message ? error.message : error),
      };
      console.log(JSON.stringify({ event: "kaggle_push_error", mode: attempt, detail: last.detail }));
      if (mode !== "auto") break;
    }
  }

  return last || { ok: false, mode: order[0], status: 0, detail: "push was not attempted" };
}

/* ========================================================================== *
 * Telegram transport
 * ========================================================================== */

async function sendTelegram(env, chatId, text, replyToMessageId) {
  const token = requireEnv(env, "TELEGRAM_BOT_TOKEN");
  const payload = { chat_id: chatId, text };
  if (replyToMessageId) payload.reply_to_message_id = replyToMessageId;

  try {
    const response = await fetch(`${TELEGRAM_API}/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(TELEGRAM_TIMEOUT_MS),
    });
    if (!response.ok) {
      console.log(
        JSON.stringify({
          event: "telegram_reply_failed",
          status: response.status,
          body: truncate(await response.text(), 300),
        })
      );
    }
    return response.ok;
  } catch (error) {
    console.log(
      JSON.stringify({
        event: "telegram_reply_failed",
        detail: truncate(error && error.message ? error.message : error),
      })
    );
    return false;
  }
}

/* ========================================================================== *
 * Update handling
 * ========================================================================== */

function chatIsAllowed(env, chatId) {
  const allowed = listEnv(env.ALLOWED_CHAT_IDS);
  if (allowed.length === 0) return true;
  return allowed.includes(String(chatId));
}

/** Pull the URL out of the command, falling back to the replied-to message. */
function extractTarget(message) {
  const text = String(message.text || "").trim();
  const parts = text.split(/\s+/);
  const command = parts.shift(); // "/dub" or "/dub@mybot"
  const rest = parts;

  let urlToken = null;
  let argTokens = [];
  for (const token of rest) {
    if (!urlToken && /^https?:\/\//i.test(token)) {
      urlToken = token;
      continue;
    }
    if (urlToken) argTokens.push(token);
  }

  if (!urlToken && message.reply_to_message && typeof message.reply_to_message.text === "string") {
    const quoted = message.reply_to_message.text.split(/\s+/).find((t) => /^https?:\/\//i.test(t));
    if (quoted) urlToken = quoted;
  }

  return { command, urlToken, argTokens };
}

function validateExtraArgs(tokens) {
  if (tokens.length > MAX_EXTRA_ARGS) {
    throw new UserFacingError(`Too many extra arguments (max ${MAX_EXTRA_ARGS}).`);
  }
  for (const token of tokens) {
    if (!ARG_TOKEN_RE.test(token)) {
      throw new UserFacingError(`Unsupported argument: ${truncate(token, 40)}`);
    }
  }
  return tokens;
}

async function handleDub(env, message) {
  const chatId = message.chat && message.chat.id;

  if (!chatIsAllowed(env, chatId)) {
    await sendTelegram(env, chatId, "⛔ This chat is not authorised to drive the Moon Mission.", message.message_id);
    return { handled: "denied_chat" };
  }

  const { urlToken, argTokens } = extractTarget(message);
  const videoUrl = validateVideoUrl(urlToken, env);
  const extraArgs = validateExtraArgs(argTokens);

  // Fail fast on configuration problems, and say so in Telegram.
  requireEnv(env, "KAGGLE_USERNAME");
  requireEnv(env, "KAGGLE_KEY");
  const kernel = resolveKernel(env);
  const metadata = buildKernelMetadata(env, kernel);
  const notebook = buildNotebook(env, kernel, videoUrl, extraArgs);

  const result = await pushToKaggle(env, metadata, notebook);
  const kernelUrl = `https://www.kaggle.com/code/${kernel.id}`;

  if (!result.ok) {
    await sendTelegram(
      env,
      chatId,
      [
        "⚠️ Kaggle Moon Mission push failed.",
        `Mode: ${result.mode} · HTTP ${result.status || "n/a"}`,
        `Reason: ${truncate(result.detail, 400)}`,
      ].join("\n"),
      message.message_id
    );
    return { status: 200, handled: "push_failed" };
  }

  await sendTelegram(env, chatId, SUCCESS_TEXT, message.message_id);

  if (isEnabled(env.SEND_KERNEL_LINK)) {
    await sendTelegram(
      env,
      chatId,
      `🔗 Kernel: ${kernelUrl}\n📦 Target: ${videoUrl}\n⚙️ Push mode: ${result.mode}`,
      message.message_id
    );
  }

  return { status: 200, handled: "pushed", kernel: kernel.id, mode: result.mode };
}

async function handleUpdate(env, update) {
  const message = update.message || update.edited_message || update.channel_post;
  if (!message || typeof message.text !== "string" || !message.text.trim()) {
    return { status: 200, handled: "ignored" };
  }

  const chatId = message.chat && message.chat.id;
  const command = message.text.trim().split(/\s+/)[0].split("@")[0].toLowerCase();

  if (command === "/start" || command === "/help") {
    await sendTelegram(env, chatId, HELP_TEXT, message.message_id);
    return { status: 200, handled: "help" };
  }

  if (command === "/dub") {
    try {
      return await handleDub(env, message);
    } catch (error) {
      const text =
        error instanceof ConfigError
          ? `🛠 Worker configuration error: ${error.message}`
          : error instanceof UserFacingError
            ? error.message
            : `💥 Unexpected error: ${truncate(error && error.message ? error.message : error, 400)}`;

      console.log(
        JSON.stringify({
          event: "dub_error",
          kind: error instanceof ConfigError ? "config" : error instanceof UserFacingError ? "user" : "internal",
          detail: truncate(text, 400),
        })
      );

      // Always acknowledge Telegram (200) so it does not retry a poisoned update.
      await sendTelegram(env, chatId, `⚠️ ${text}`, message.message_id);
      return { status: 200, handled: "error" };
    }
  }

  return { status: 200, handled: "ignored" };
}

/* ========================================================================== *
 * Worker entrypoint
 * ========================================================================== */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" || request.method === "HEAD") {
      if (url.pathname === "/" || url.pathname === "/health") {
        return jsonResponse({
          ok: true,
          service: "t-dubber-postman",
          endpoint: "/",
          push_mode: env.KAGGLE_PUSH_MODE || "auto",
        });
      }
      return jsonResponse({ ok: false, error: "not found" }, 404);
    }

    if (request.method !== "POST") {
      return jsonResponse({ ok: false, error: "method not allowed" }, 405, { allow: "GET, HEAD, POST" });
    }

    // Telegram signs every webhook with the secret configured at setWebhook time.
    const secret = (env.TELEGRAM_SECRET_TOKEN || "").trim();
    if (secret) {
      const presented = request.headers.get("x-telegram-bot-api-secret-token") || "";
      if (!secretsMatch(presented, secret)) {
        console.log(JSON.stringify({ event: "webhook_rejected", reason: "bad_secret" }));
        return jsonResponse({ ok: false, error: "unauthorised" }, 401);
      }
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return jsonResponse({ ok: false, error: "body must be Telegram update JSON" }, 400);
    }
    if (!update || typeof update !== "object") {
      return jsonResponse({ ok: false, error: "body must be Telegram update JSON" }, 400);
    }

    try {
      const outcome = await handleUpdate(env, update);
      // Telegram treats any 2xx as "delivered"; the body carries the detail.
      return jsonResponse({ ok: true, ...outcome }, outcome.status || 200);
    } catch (error) {
      console.log(
        JSON.stringify({
          event: "update_failed",
          detail: truncate(error && error.message ? error.message : error, 400),
        })
      );
      return jsonResponse({ ok: false, error: "internal error" }, 500);
    }
  },
};
