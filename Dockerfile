# Serve-only image: login needs a desktop browser and OS keyring, so it stays on the host.
FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv

FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS builder
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

FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170
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
