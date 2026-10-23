FROM python:3.12.14-alpine3.24@sha256:4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PAYMENTS_DATABASE_PATH=/data/payments.db
WORKDIR /app
COPY requirements-runtime.txt /app/requirements-runtime.txt
RUN apk upgrade --no-cache \
    && python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements-runtime.txt \
    && python -m pip uninstall -y pip \
    && mkdir /data && chown 10001:10001 /data
COPY --chown=10001:10001 payments_api /app/payments_api
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "payments_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
