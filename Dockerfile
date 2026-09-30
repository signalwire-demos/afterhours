FROM python:3.11-slim

WORKDIR /app

# Install curl for healthcheck
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Copy requirements first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# .git is excluded from the build context (see .dockerignore), so the commit is
# passed in and written to a file the app can read. Production sets GIT_REV at
# runtime instead, which takes precedence.
ARG GIT_COMMIT=""
RUN if [ -n "$GIT_COMMIT" ]; then printf '%s' "$GIT_COMMIT" > COMMIT; fi

# Default port
ENV PORT=5000

EXPOSE ${PORT}

RUN useradd -r -m -s /bin/false appuser && chown -R appuser:appuser /app
USER appuser

CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT} --workers 2 --preload --worker-class uvicorn.workers.UvicornWorker"]
