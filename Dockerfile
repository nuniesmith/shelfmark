# syntax=docker/dockerfile:1

FROM python:3.13-slim

ARG PUID=1001
ARG PGID=1001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

# The Python standard library handles zip/tar archives, and 7zip adds 7z.
#
# RAR needs the real (non-free) `unrar`. Scene RARs use compression methods
# that neither 7zip ("Unsupported Method") nor unrar-free (misparses the
# archive header, extracts nothing, reports success) can decompress -- a
# book failed import that way, surfaced as a misleading UTF-8 codec error.
# `unrar` lives in Debian's non-free component, enabled below; if that sed
# ever stops matching the base image's sources, `apt-get install unrar`
# fails the build loudly rather than shipping an image without it.
#
# rsync and openssh-client are what the worker uses to pull completed downloads
# off Sullivan — transfer.py shells out to `rsync -e "ssh -i ..."`. They were
# missing from the first image, which the unit tests could not catch because
# they inject a fake subprocess runner. The failure only appears on a real
# transfer, as `rsync: not found`.
RUN sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install --yes --no-install-recommends \
        7zip \
        openssh-client \
        rsync \
        unrar \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid "${PGID}" shelfmark \
    && useradd --uid "${PUID}" --gid "${PGID}" --create-home \
        --home-dir /home/shelfmark --shell /usr/sbin/nologin shelfmark

WORKDIR /app

RUN mkdir --parents /data

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY LICENSE README.md pyproject.toml ./

# The sync-account check has to run from the side that holds the private key,
# which is this container. Shipping it on PATH is what makes it runnable as
# `docker exec shelfmark-worker verify-sullivan-sync` after provisioning.
COPY scripts/verify-sullivan-sync.sh /usr/local/bin/verify-sullivan-sync
RUN chmod 0755 /usr/local/bin/verify-sullivan-sync

RUN python -m pip install --no-cache-dir --no-deps . \
    && chown --recursive shelfmark:shelfmark /app /data

USER shelfmark

# The installed console scripts let Compose run the CLI, API, or worker from
# the same image. The CLI remains the default for backwards-compatible use.
ENTRYPOINT []
CMD ["shelfmark", "--help"]
