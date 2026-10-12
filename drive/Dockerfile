# syntax=docker/dockerfile:1

# ─────────────────────────────────────────────────────────────────────────────
# Drive app — CPU-only, no CUDA, no PyTorch.
#
# Multi-stage so the runtime image carries no build toolchain, no npm cache and
# no devDependencies. The final image runs `node server.js` and nothing else.
#
#   docker build -t drive .
#   docker run -p 3000:3000 \
#     -e DATABASE_URL="postgresql://…" \
#     -e NEXT_PUBLIC_SUPABASE_URL="https://….supabase.co" \
#     -e SUPABASE_SERVICE_ROLE_KEY="…" \
#     drive
# ─────────────────────────────────────────────────────────────────────────────

# ── deps: install exactly what package.json locks ────────────────────────────
FROM node:22-alpine AS deps
WORKDIR /app

# libc6-compat is what sharp (Next's image optimiser) links against.
RUN apk add --no-cache libc6-compat

COPY package.json package-lock.json ./
RUN npm ci

# ── builder: produce the standalone server ───────────────────────────────────
FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# ── runner: the small part ───────────────────────────────────────────────────
FROM node:22-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

RUN apk add --no-cache libc6-compat

# `standalone` already contains a pruned node_modules and a server.js entry
# point. Everything below is the static half that is NOT bundled into it.
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

# Run unprivileged. The node image ships a `node` user (uid 1000) already.
USER node

EXPOSE 3000

# `/` serves the captured drive UI, so a 200 here means the whole app booted.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.js"]