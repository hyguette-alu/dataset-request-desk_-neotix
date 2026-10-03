FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# curl is only needed for the container healthcheck
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Dev dependencies are installed into the image so `make test` can run the
# suite inside the same container. A production image would install only
# requirements.txt.
COPY requirements.txt requirements-dev.txt ./
# BuildKit cache mount: downloaded wheels survive between builds, so changing
# one pinned version does not re-fetch the whole dependency set. The cache
# lives outside the image, so it does not add to the final size.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 120 --retries 10 \
        -r requirements.txt -r requirements-dev.txt

COPY . .
RUN chmod +x entrypoint.sh

EXPOSE 8000
# Invoked through bash rather than as ./entrypoint.sh: the compose file bind
# mounts ./api over /app in development, which replaces the executable bit set
# above with whatever the host file has.
CMD ["bash", "entrypoint.sh"]
