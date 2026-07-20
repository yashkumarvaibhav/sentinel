# Web front door: builds the app, then serves it from Caddy along with the
# reverse proxy to the gateway. Build context is the repository root.
FROM node:24-alpine AS build

WORKDIR /app
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
ARG SENTINEL_GIT_SHA=unknown
ENV VITE_GIT_SHA=${SENTINEL_GIT_SHA}
RUN npm run build

FROM caddy:2.8-alpine
COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
EXPOSE 8041
