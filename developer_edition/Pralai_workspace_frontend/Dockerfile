# Workspace web image: the Module-Federation remote the Control Portal mounts
# (remoteEntry.js + hashed chunks), served as static files by nginx.
#
# In the data plane Envoy routes /workspace/ here (prefix rewritten to /) and
# /workspace-api/ to the backend, and injects the identity headers, so this
# server proxies nothing: see nginx.dataplane.conf. The standalone index.html
# build ships in the same dist/ and still works on its own.
#
# node:20, same as CI (.github/workflows/ci.yml) and docker-compose.yml.
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.dataplane.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
