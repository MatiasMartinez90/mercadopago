FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --prefix=/install .

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/home/app/.local/bin:$PATH

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app
WORKDIR /app
COPY --from=builder /install /usr/local
COPY --chown=app:app src ./src
COPY --chown=app:app db ./db
USER 10001:10001

EXPOSE 8080
CMD ["uvicorn", "mercadopago_service.main:app", "--host", "0.0.0.0", "--port", "8080"]
