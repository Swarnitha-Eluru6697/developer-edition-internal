# ML Studio web — the Module-Federation REMOTE container.
#
# The Control Portal fetches /assets/remoteEntry.js from here through the
# tunnel, so this image's whole job is to serve the built bundle. It proxies
# nothing: Envoy routes the SPA's /api calls to the ML Studio backend. Mirrors
# the UCS catalog web image, which the portal already loads the same way.
FROM node:20-alpine AS build
WORKDIR /web
COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY . .

# Vite inlines VITE_* at build time — there is no way to inject these into a
# finished bundle at container start, so they have to arrive as build args.
# Served under a path prefix (e.g. /mlstudio/), the built index.html would
# otherwise ask for /assets/… at the root and load nothing.
ARG VITE_BASE_PATH=/
ENV VITE_BASE_PATH=$VITE_BASE_PATH
# Optional: VITE_UCS_ENABLED=false (no Unified Catalog) hides the model Lineage tab.
ARG VITE_UCS_ENABLED
ENV VITE_UCS_ENABLED=$VITE_UCS_ENABLED

RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /web/dist /usr/share/nginx/html
# Static only: in the data plane Envoy routes /mlstudio/api/ to the backend and
# injects identity, so this image proxies nothing (nginx.dataplane.conf).
# nginx.conf (also static only) is the compose variant.
COPY nginx.dataplane.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
