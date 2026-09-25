# syntax=docker/dockerfile:1

FROM node:20-slim AS base
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-lock.yaml* pnpm-workspace.yaml* ./
RUN pnpm install --frozen-lockfile || pnpm install

# ---- dev: pnpm dev with hot reload, source bind-mounted by docker-compose ----
FROM base AS dev
COPY . .
EXPOSE 5173
CMD ["pnpm", "dev", "--host"]

# ---- build: produce the static bundle ----
FROM base AS build
ARG VITE_API_URL
ENV VITE_API_URL=${VITE_API_URL}
COPY . .
RUN pnpm build

# ---- prod: serve the static bundle ----
FROM node:20-slim AS prod
WORKDIR /app
RUN npm install -g serve
COPY --from=build /app/dist ./dist
EXPOSE 5173
CMD ["serve", "-s", "dist", "-l", "5173"]
