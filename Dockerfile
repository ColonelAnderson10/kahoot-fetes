# Image légère pour l'application Kahoot des fêtes
FROM python:3.12-slim

# Pas de .pyc (système de fichiers en lecture seule), journaux non tamponnés
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# pillow-heif est distribué en roue « manylinux » qui embarque déjà libheif et ses codecs :
# aucune bibliothèque système supplémentaire n'est nécessaire. On applique seulement les
# mises à jour de sécurité du système de base.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Utilisateur non-root. UID 99 / GID 100 = nobody:users, la convention d'Unraid pour appdata.
RUN useradd --uid 99 --gid 100 --no-create-home --shell /usr/sbin/nologin kahoot

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /app/data && chown 99:100 /app/data
USER 99:100

EXPOSE 8000

# Vérification de santé sans outil externe (pas de curl dans l'image)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

# --proxy-headers : uvicorn lit X-Forwarded-For / X-Forwarded-Proto
# --forwarded-allow-ips 127.0.0.1 : seulement depuis le proxy Tailscale, qui partage le réseau du conteneur
CMD ["uvicorn", "app.main:app_factory", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1", \
     "--no-server-header", "--workers", "1"]
