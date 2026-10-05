# Serve-only image: login needs a desktop browser and OS keyring, so it stays on the host.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    M365_SESSION_FILE=/run/m365/session.json

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# Optional corporate CA bundle for TLS-inspecting networks (BuildKit secret, never stored in a layer).
RUN --mount=type=secret,id=ca,required=false \
    if [ -s /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi \
    && pip install --index-url https://pypi.org/simple . \
    && useradd --create-home --uid 10001 gateway

USER gateway
EXPOSE 47821
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:47821/health', timeout=4)"
ENTRYPOINT ["m365-agent-gateway"]
CMD ["serve", "--host", "0.0.0.0", "--port", "47821"]
