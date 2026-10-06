FROM python:3.11-slim AS build

RUN groupadd --system --gid 10001 agent \
  && useradd --system --uid 10001 --gid agent --home-dir /app --shell /usr/sbin/nologin agent

WORKDIR /app
COPY server ./server
COPY public/geo ./public/geo
RUN pip install --no-cache-dir uv==0.11.8 \
  && cd server \
  && uv sync --frozen --no-dev --no-editable

ENV PATH="/app/server/.venv/bin:${PATH}"

ENV SQLITE_PATH=/data/agent.db
RUN mkdir -p /data \
  && chown agent:agent /data

COPY scripts/cloud/assemble_python_runtime.py /assemble_python_runtime.py
RUN python /assemble_python_runtime.py

FROM scratch AS backend
COPY --from=build /runtime/ /
ENV PATH="/app/server/.venv/bin:/usr/local/bin:/usr/bin:/bin"
ENV LD_LIBRARY_PATH=/usr/local/lib
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt SSL_CERT_DIR=/etc/ssl/certs
ENV OPENSSL_CONF=/etc/ssl/openssl.cnf
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SQLITE_PATH=/data/agent.db
WORKDIR /app
USER 10001:10001

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=3).read()"
CMD ["/bin/sh", "-c", "set -eu; if [ \"${APP_ENV:-}\" != \"staging\" ] && [ \"${APP_ENV:-}\" != \"production\" ]; then echo 'Refusing to start: APP_ENV must be staging or production for the Docker image.' >&2; exit 1; fi; if [ \"${ENFORCE_INTERNAL_TOKEN:-}\" != \"1\" ]; then echo 'Refusing to start: ENFORCE_INTERNAL_TOKEN=1 is required for the Docker image.' >&2; exit 1; fi; if [ -z \"${INTERNAL_API_TOKEN:-}\" ]; then echo 'Refusing to start: INTERNAL_API_TOKEN must be set for the Docker image.' >&2; exit 1; fi; export SQLITE_PATH=\"${SQLITE_PATH:-/data/agent.db}\"; exec uvicorn app.main:app --app-dir server --host 0.0.0.0 --port 8000"]
