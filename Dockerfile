# syntax=docker/dockerfile:1

FROM python:3.13-slim

ARG PUID=1001
ARG PGID=1001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

# The Python standard library handles zip/tar archives. 7zip and unrar-free add
# the RAR and 7z support the local CLI accepts.
#
# rsync and openssh-client are what the worker uses to pull completed downloads
# off Sullivan — transfer.py shells out to `rsync -e "ssh -i ..."`. They were
# missing from the first image, which the unit tests could not catch because
# they inject a fake subprocess runner. The failure only appears on a real
# transfer, as `rsync: not found`.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        7zip \
        openssh-client \
        rsync \
        unrar-free \
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
RUN python -m pip install --no-cache-dir --no-deps . \
    && chown --recursive shelfmark:shelfmark /app /data

USER shelfmark

# The installed console scripts let Compose run the CLI, API, or worker from
# the same image. The CLI remains the default for backwards-compatible use.
ENTRYPOINT []
CMD ["shelfmark", "--help"]
