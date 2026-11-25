# ---------- build stage: compile the React app to static files
FROM node:22-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# Optional build-time default; the runtime API_BASE_URL env var (see below) wins.
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

# ---------- runtime stage: static files served by nginx (~50 MB image)
FROM nginx:1.27-alpine AS runtime
ENV API_BASE_URL=http://localhost:8000
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint.d/40-app-config.sh /docker-entrypoint.d/40-app-config.sh
RUN chmod +x /docker-entrypoint.d/40-app-config.sh
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 3000
