.PHONY: up down logs test build clean dev-backend dev-frontend

# Start the whole stack: backend on :8000, frontend on :3000.
up: .env
	docker compose up -d --build
	@echo "frontend: http://localhost:3000   backend: http://localhost:8000/health"

down:
	docker compose down

logs:
	docker compose logs -f

build:
	docker compose build

# Remove containers AND the SQLite/cache volume.
clean:
	docker compose down -v

# Backend test suite (uses a local fake provider, no real API keys needed).
test:
	cd backend && python -m pip install -q -r requirements-dev.txt && python -m pytest -q

dev-backend:
	cd backend && set -a && . ../.env && set +a && uvicorn app.main:app --reload --port 8000

dev-frontend:
	cd frontend && npm install && npm run dev

.env:
	@echo "No .env found - creating one from .env.example (put your real keys in it)."
	cp .env.example .env
