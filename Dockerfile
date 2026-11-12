# Repo Guide container image.
#
#   docker build -t repo-guide .
#   docker run --rm -p 8000:8000 --env-file .env repo-guide
#
# Then open http://localhost:8000

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# 1) Install dependencies first, in their own layer. This layer is only rebuilt
#    when pyproject.toml changes, so code-only changes build in seconds.
COPY pyproject.toml README.md ./
RUN mkdir app && touch app/__init__.py \
    && pip install . \
    && pip uninstall -y repo-guide \
    && rm -rf app build *.egg-info

# 2) Then copy the actual code.
COPY app ./app

# Don't run as root: if the app were ever compromised, it can't touch the system.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# Lets Docker (and hosts that read it) know when the app is actually ready.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + __import__('os').environ.get('PORT', '8000') + '/health')"

# Hosting platforms like Render pass the port in $PORT. --proxy-headers makes
# rate limiting see each visitor's real IP instead of the platform's proxy.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
