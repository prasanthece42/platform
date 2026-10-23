FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PAYMENTS_DATABASE_PATH=/data/payments.db
WORKDIR /app
COPY requirements-runtime.txt /app/requirements-runtime.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements-runtime.txt \
    && mkdir /data && chown 10001:10001 /data
COPY --chown=10001:10001 payments_api /app/payments_api
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "payments_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
