FROM python:3.13-slim-trixie AS base
WORKDIR /app
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends util-linux \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools && pip install --no-cache-dir -r requirements.txt
RUN groupadd -g 1000 kapowarr && useradd -u 1000 -g kapowarr -d /nonexistent -M -s /bin/bash kapowarr \
    && mkdir -p /app/db /app/logs /app/temp_downloads

FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY . .
RUN chmod 755 backend/lib/unrar_linux_64
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-p", "*.py"]

FROM base AS runtime
RUN python -m pip uninstall -y pip setuptools wheel
LABEL org.opencontainers.image.title="Pullarr" \
      org.opencontainers.image.description="Manage, monitor and organize your comic library" \
      org.opencontainers.image.licenses="GPL-3.0 AND LicenseRef-UnRAR AND MIT"
COPY backend backend
COPY frontend frontend
COPY scripts/archive_maintenance_container_smoke.py scripts/archive_maintenance_container_smoke.py
COPY Pullarr.py Kapowarr.py pyproject.toml LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
COPY licenses licenses
COPY --chmod=755 entrypoint.sh .
RUN chmod 755 backend/lib/unrar_linux_64
ENV PUID=0 PGID=0 TZ=UTC PYTHONDONTWRITEBYTECODE=1
EXPOSE 5656
# Liveness only: local non-sensitive static resource, not a database readiness claim.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5656/static/img/favicon.svg', timeout=3)"
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "/app/Pullarr.py", "--Host", "0.0.0.0", "--LogFolder", "/app/logs"]
