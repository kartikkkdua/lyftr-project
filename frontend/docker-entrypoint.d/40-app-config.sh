#!/bin/sh
# Write runtime config from environment variables, so one image works for any backend URL.
set -e
escaped=$(printf '%s' "${API_BASE_URL:-http://localhost:8000}" | sed 's/["\\]/\\&/g')
cat > /usr/share/nginx/html/config.js <<CFG
window.__APP_CONFIG__ = { API_BASE_URL: "${escaped}" };
CFG
echo "app config: API_BASE_URL=${API_BASE_URL}"
