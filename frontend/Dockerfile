# The §13 screens, built and served by Next.
#
# Multi-stage so the runtime image carries the build output and the production
# dependency tree, not the toolchain that produced them.

FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
# `ci`, not `install`: the lockfile is the build's input, and a container that
# silently resolved a different React than the laptop did would be a bug that
# only reproduces in Docker.
RUN npm ci

FROM node:22-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# API_URL is a **build-time** value, and finding that out cost a boot.
#
# `next.config.ts` looks like it resolves the proxy target per request, and
# `next start` does re-run the config file — but `rewrites()` is evaluated during
# `next build` and its destination is frozen into `.next/routes-manifest.json`,
# which is what the production server actually reads. Setting API_URL only in the
# compose `environment:` produced a container that started cleanly and then
# proxied every call to `127.0.0.1:8000` — the fallback baked in at build time —
# giving ECONNREFUSED on a service that was up the whole time.
#
# So it is an ARG, defaulted to the compose service name. Override it for any
# other topology: `--build-arg API_URL=http://elsewhere:8000`.
ARG API_URL=http://api:8000
ENV API_URL=$API_URL

RUN npm run build

FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production
COPY --from=deps /app/node_modules ./node_modules
COPY --from=build /app/.next ./.next
COPY package.json next.config.ts tsconfig.json ./
COPY app ./app
COPY lib ./lib

EXPOSE 3000
CMD ["npm", "run", "start"]
