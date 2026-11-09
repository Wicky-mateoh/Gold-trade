# Stage 1: build the React dashboard.
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# Stage 2: API + static dashboard.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY db ./db
COPY data ./data
COPY --from=web /web/dist ./frontend/dist

ENV PYTHONPATH=/app/backend \
    GITS_ROOT=/app
RUN useradd --create-home gits && mkdir -p /app/var && chown -R gits /app/var
USER gits
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
