# Serve-only image: login needs a desktop browser and OS keyring, so it stays on the host.
FROM ghcr.io/astral-sh/uv:0.11.26@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5 AS uv

FROM python:3.12-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
# Optional corporate CA bundle for TLS-inspecting networks (BuildKit secret, never stored in a layer).
# Locked runtime dependencies only: no dev extras, and no Playwright (login stays on the host).
RUN --mount=type=secret,id=ca,required=false \
    if [ -s /run/secrets/ca ]; then export SSL_CERT_FILE=/run/secrets/ca; fi \
    && uv sync --locked --no-dev --no-install-project --no-install-package playwright
COPY src ./src
RUN --mount=type=secret,id=ca,required=false \
    if [ -s /run/secrets/ca ]; then export SSL_CERT_FILE=/run/secrets/ca; fi \
    && uv sync --locked --no-dev --no-editable --no-install-package playwright

FROM python:3.12-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    M365_SESSION_FILE=/run/m365/session.json
COPY --from=builder /opt/venv /opt/venv
RUN useradd --create-home --uid 10001 gateway
USER gateway
EXPOSE 47821
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:47821/health', timeout=4)"
ENTRYPOINT ["m365-agent-gateway"]
CMD ["serve", "--host", "0.0.0.0", "--port", "47821"]
