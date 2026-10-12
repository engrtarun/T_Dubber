/**
 * Generate `public/t9-cloud/` — the T9 Cloud branded build — from the pristine
 * capture in `public/drive-ui/`.
 *
 * WHY A GENERATOR AND NOT EDITED FILES
 * `public/drive-ui/` is the captured OneDrive UI and stays byte-for-byte
 * untouched — it is the reference, and a commit can prove it with a hash. All
 * T9 branding therefore has to be derived, not applied in place. Running this
 * script again after a fresh capture re-applies every brand change in one
 * command, which is the only way "we changed the logo" and "the capture is
 * still pristine" can both be true.
 *
 * WHAT IT DOES (four changes, each reversible by deleting the output folder)
 *   1. Dark theme by default — the capture already ships BOTH Fluent palettes
 *      scoped by [data-theme=…], so flipping the body attribute activates dark
 *      instead of hand-writing a theme.
 *   2. Removes the "Get 100 GB free" upsell — storage is Telegram's job, not a
 *      paid quota (see root README §6.5).
 *   3. Removes the storage-Buy prompts for the same reason.
 *   4. Renames the product from OneDrive to T9 Cloud.
 *
 * Usage:  npm run brand
 */
import { readdir, readFile, writeFile, mkdir, rm, copyFile, stat } from "node:fs/promises";
import { join, relative, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const SRC = join(ROOT, "public", "drive-ui");
const OUT = join(ROOT, "public", "t9-cloud");
const BRAND = "T9 Cloud";

/** Recursively list every file under `dir`. */
async function walk(dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await walk(full)));
    else out.push(full);
  }
  return out;
}

// ── the brand layer ─────────────────────────────────────────────────────────

const CSS = `/* T9 Cloud brand layer — injected by scripts/brand-t9.mjs.
   The capture below is untouched; everything here is additive. */

/* 1. Dark by default. The capture already contains both Fluent palettes scoped
      by [data-theme]; this only forces the attribute before first paint. */
html[data-theme="fluent-light"] { color-scheme: dark; }

/* 2/3. Storage upsells are meaningless here — bytes live on Telegram, which is
        effectively unlimited, so a 5 GB quota with a "Buy storage" button is a
      lie. Remove the whole quota banner and the nav purchase prompts. */
.od-QuotaBannerWrapper,
.od-QuotaBanner-upsell,
.od-QBannerUpsell-main,
.od-QBannerUpsell-barBtn,
.od-QBannerUpsell-freeTrialCTA,
#LeftNavStorageButton,
.buyStorageButton_7ff103ac,
.getMoreStorageButton_7ff103ac,
.storageButtonText_7ff103ac,
[data-automationid="getMoreStorageButton"],
[data-automationid="upgradeButton"] { display: none !important; }

/* Give the freed banner height back to the content instead of leaving a gap. */
.od-QuotaBannerWrapper-placeholder { flex: 0 !important; }

/* 4. Product rename. The capture's own markup still says OneDrive; this is a
      belt-and-braces layer for any element the script in <head> missed. */
.t9-rebrand { font-weight: 600; letter-spacing: -0.01em; }

/* The account name sits where a brand lockup would be — T9 Cloud gets a mark. */
.t9-cloud-mark {
  display: inline-flex; align-items: center; gap: 8px;
  font-weight: 700; letter-spacing: -0.02em;
}
.t9-cloud-mark::before {
  content: ""; width: 10px; height: 10px; border-radius: 3px;
  background: linear-gradient(135deg, #0f6cbd, #4a92ff);
  box-shadow: 0 0 0 1px rgba(255,255,255,.18) inset;
}
`;

const JS = `/* T9 Cloud brand layer — injected by scripts/brand-t9.mjs */
(function () {
  var BRAND = ${JSON.stringify(BRAND)};

  // Dark first, before anything paints, so there is no white flash.
  function dark() {
    document.documentElement.setAttribute("data-theme", "fluent-dark");
    document.body && document.body.setAttribute("data-theme", "fluent-dark");
  }
  dark();
  document.addEventListener("DOMContentLoaded", dark);

  function rebrand() {
    document.title = document.title.replace(/OneDrive/g, BRAND);

    // Text nodes only — never touch attributes, or links break.
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    var nodes = [], n;
    while ((n = walker.nextNode())) nodes.push(n);
    nodes.forEach(function (node) {
      if (node.nodeValue && node.nodeValue.indexOf("OneDrive") !== -1) {
        node.nodeValue = node.nodeValue.replace(/OneDrive/g, BRAND);
      }
    });

    // Storage upsells: gone at the DOM level too, not just hidden by CSS.
    // The left-nav purchase button is matched by id AND class — the capture
    // gives it no data-automationid, which is why a selector written for the
    // banner alone silently misses it.
    document
      .querySelectorAll(
        ".od-QuotaBannerWrapper, .od-QBannerUpsell-main, .od-QBannerUpsell-barBtn, " +
          "#LeftNavStorageButton, .buyStorageButton_7ff103ac, " +
          ".getMoreStorageButton_7ff103ac, " +
          '[data-automationid="getMoreStorageButton"], ' +
          '[data-automationid="upgradeButton"]'
      )
      .forEach(function (el) { el.remove(); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", rebrand);
  } else {
    rebrand();
  }

  // The capture boots a React tree that can re-render these nodes after load.
  new MutationObserver(rebrand).observe(document.documentElement, {
    childList: true, subtree: true,
  });
})();
`;

const HEAD_INJECT = `<link rel="stylesheet" href="/t9-cloud/t9.css"><script src="/t9-cloud/t9.js"></script>`;

/**
 * Remove a complete `<div>` subtree that starts at `start`.
 *
 * A regex cannot do this reliably: the banner nests several `<div>`s deep, so
 * `[\s\S]*?</div>` stops at the FIRST close tag and leaves the rest of the
 * markup dangling — which renders as broken layout, not as a removed banner.
 * Counting opens against closes is the only version that actually removes it.
 */
function removeDivAt(html, start) {
  let depth = 0;
  let i = start;
  while (i < html.length) {
    const open = html.indexOf("<div", i);
    const close = html.indexOf("</div>", i);
    if (close === -1) break; // unterminated — leave the document alone
    if (open !== -1 && open < close) {
      depth += 1;
      i = open + 4;
    } else {
      depth -= 1;
      i = close + 6;
      if (depth === 0) return html.slice(0, start) + html.slice(i);
    }
  }
  return html;
}

/** Remove every quota-banner / storage-upsell subtree. */
function stripStorageUpsells(html) {
  let out = html;
  const roots = [
    '<div class="od-QuotaBannerWrapper"',
    '<div class="od-QBannerUpsell-main',
    '<div class="od-QBannerUpsell-barBtn',
  ];
  for (const marker of roots) {
    let from = 0;
    while (true) {
      const at = out.indexOf(marker, from);
      if (at === -1) break;
      const before = out.lastIndexOf("<", at);
      const next = removeDivAt(out, before === -1 ? at : before);
      if (next === out) { from = at + marker.length; continue; }
      out = next;
      from = at;
    }
  }
  // The nav-level purchase prompts are buttons, not wrappers. The capture
  // identifies them by id/class, not data-automationid.
  return out.replace(
    /<button[^>]*id="LeftNavStorageButton"[\s\S]*?<\/button>\s*<\/div>/gi,
    "",
  );
}

// ── generate ────────────────────────────────────────────────────────────────

async function main() {
  try { await stat(SRC); } catch {
    console.error(`Capture not found at ${SRC}`);
    process.exit(2);
  }

  await rm(OUT, { recursive: true, force: true });
  await mkdir(OUT, { recursive: true });

  const files = await walk(SRC);
  let patched = 0;

  for (const file of files) {
    const rel = relative(SRC, file);
    const dest = join(OUT, rel);
    await mkdir(dirname(dest), { recursive: true });

    if (!file.endsWith(".html")) {
      await copyFile(file, dest);
      continue;
    }

    let html = await readFile(file, "utf8");

    // 1. Dark by default.
    html = html.replace(/data-theme="fluent-light"/g, 'data-theme="fluent-dark"');
    // 4. Cheap server-side rename so the very first paint is already branded.
    html = html.replace(/OneDrive/g, BRAND);
    // 2/3. Drop the quota banner and storage upsells outright.
    html = stripStorageUpsells(html);

    // Inject the layer last, so nothing above can match inside it.
    if (html.includes("</head>")) html = html.replace("</head>", `${HEAD_INJECT}</head>`);
    else html = HEAD_INJECT + html;

    await writeFile(dest, html, "utf8");
    patched += 1;
  }

  await writeFile(join(OUT, "t9.css"), CSS, "utf8");
  await writeFile(join(OUT, "t9.js"), JS, "utf8");

  console.log(`T9 Cloud built -> public/t9-cloud/`);
  console.log(`  ${files.length} files copied from the capture, ${patched} HTML patched`);
  console.log(`  dark theme + no storage upsell + brand "${BRAND}"`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});