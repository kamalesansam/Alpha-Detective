.PHONY: setup samples backend frontend dev test

setup:
	cd backend && python3 -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -r requirements.txt
	cd backend && [ -f .env ] || cp .env.example .env
	cd backend && .venv/bin/python scripts/make_samples.py
	cd frontend && npm install
	@echo ""
	@echo "Setup complete. Put your free Gemini key in backend/.env (GOOGLE_API_KEY=...) — or run keyless."
	@echo "Start the app with:  make dev   → http://localhost:3000"

samples:
	cd backend && .venv/bin/python scripts/make_samples.py

backend:
	cd backend && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

frontend:
	cd frontend && npm run dev

test:
	cd backend && .venv/bin/python -m pytest tests -q
	cd frontend && npm run build

dev:
	@trap 'kill 0' INT TERM; \
	( cd backend && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 ) & \
	( cd frontend && npm run dev ) & \
	wait
