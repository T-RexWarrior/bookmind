# BookMind / 学迹 — backend image (PRODUCTIZATION §13, M7).
#
# The built React app (frontend/dist/) is committed, so this image needs no
# Node toolchain — it copies the frontend (including dist/) and serves it at
# /ui via FastAPI's static mount + SPA fallback (see api/app.py).

FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the backend + the built frontend (dist/ is committed under frontend/).
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/

ENV PYTHONPATH=/app/backend
ENV PYTHONUTF8=1
ENV BOOKMIND_HOST=0.0.0.0
ENV BOOKMIND_PORT=18765
EXPOSE 18765

# Run the API, serving the frontend at /ui.
CMD ["python", "-m", "uvicorn", "bookmind.api.app:app", "--host", "0.0.0.0", "--port", "18765"]
