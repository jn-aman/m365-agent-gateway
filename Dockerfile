# Serve-only image: login needs a desktop browser and OS keyring, so it stays on the host.
FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS builder
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

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
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
