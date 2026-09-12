# syntax=docker/dockerfile:1

FROM python:3.13-slim

ARG PUID=1001
ARG PGID=1001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The Python standard library handles zip/tar archives. These tools add RAR
# and 7z support for the same formats accepted by the local CLI.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        7zip \
        unrar-free \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid "${PGID}" shelfmark \
    && useradd --uid "${PUID}" --gid "${PGID}" --create-home \
        --home-dir /home/shelfmark --shell /usr/sbin/nologin shelfmark

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY LICENSE README.md pyproject.toml ./
RUN chown --recursive shelfmark:shelfmark /app

USER shelfmark

ENTRYPOINT ["python", "/app/src/main.py"]
CMD ["--help"]
