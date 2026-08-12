# The backend image — one image, three roles: migrate, API, worker.
#
# One image rather than three because they share every import. The API resolves
# `Settings`, the worker runs the stages that resolve it, and `alembic upgrade`
# reads the same models; splitting them would mean three dependency sets that
# can disagree about the schema they are talking to.
#
# It carries Chromium, which is most of its size. That is not optional: §5.1's
# discovery intercepts a Maps payload from a real browser, and §6.7 measured a
# plain fetch of a Facebook Page at HTTP 400 and an Instagram profile at 605 KB
# of JavaScript containing nothing. An image without a browser would run the API
# fine and fail two of the six stages.

FROM python:3.12-slim

# The same uv the README tells you to use locally, so a container build and a
# laptop build resolve the same lockfile rather than two different ones.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    # Business names are Urdu and Arabic often enough that cp1252 is a crash
    # waiting to happen; the README says to set this before any script that
    # prints one, and a container has no profile to set it in.
    PYTHONIOENCODING=utf-8 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app

# Dependencies before source, so editing a module does not re-resolve the
# lockfile or re-download Chromium.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra browser

# `--with-deps` is the apt half. Chromium in a slim image without it starts and
# then dies on a missing shared library, which surfaces three stages later as an
# empty discovery run — §5.5's failure mode, from the infrastructure layer.
RUN playwright install --with-deps chromium

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --extra browser

# §7's raw archive. Declared so a `docker run` without the compose volume still
# writes somewhere sane rather than into the image layer.
VOLUME ["/app/data"]

EXPOSE 8000

# Overridden per service in docker-compose.yml. The API is the default because
# it is the one you would run this image alone for.
CMD ["uvicorn", "leadscraper.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
