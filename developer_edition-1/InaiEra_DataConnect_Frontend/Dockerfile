FROM node:20.20.2-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS build
WORKDIR /app
# package-lock.json pins exact resolved versions; `npm ci` installs strictly
# from it (and fails if it's out of sync with package.json) instead of
# `npm install`'s "resolve within semver range, maybe update the lockfile"
# behavior — the same image spec must not resolve different dependency
# versions on two different builds.
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json tsconfig.node.json tsconfig.server.json vite.config.ts index.html server.ts ./
COPY src ./src
RUN npm run build && npm run build:server

# ── Runtime image ────────────────────────────────────────────────────────
# Deliberately not `node:20-alpine` + `npx tsx server.ts`: that would ship
# the full Node/npm/npx toolchain, TypeScript, and tsx into the final image
# just to execute a 20-line static file server, none of which is needed once
# server.ts is compiled to plain JS. This stage installs only what
# runtime/package.json actually declares (express — pinned to the exact
# version resolved in the main package-lock.json, via its own lockfile in
# runtime/), copies the compiled server.js + the built dist/, and runs it
# with plain `node`. No TypeScript runtime, no dev tooling, no unrelated
# build-only packages (React, Vite, ...) that the main package.json also
# carries but this image never uses.
FROM node:20.20.2-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293
WORKDIR /app
COPY runtime/package.json runtime/package-lock.json ./
RUN npm ci --omit=dev
COPY --from=build /app/dist-server/server.js ./server.js
COPY --from=build /app/dist ./dist
EXPOSE 5001
CMD ["node", "server.js"]
