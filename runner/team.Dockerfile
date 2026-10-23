ARG BASE_IMAGE=aegisai-local-workflow-check-backend:latest
FROM ${BASE_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY requirements-ci.txt /opt/runner/requirements-ci.txt
RUN python -m pip install --no-cache-dir --force-reinstall --only-binary=:all: --require-hashes -r /opt/runner/requirements-ci.txt
COPY scanners.lock /opt/runner/scanners.lock
RUN python -m venv /opt/scanners && /opt/scanners/bin/pip install --no-cache-dir --require-hashes -r /opt/runner/scanners.lock
COPY gitleaks /usr/local/bin/gitleaks
COPY stage.py /opt/runner/stage.py
RUN chmod 755 /usr/local/bin/gitleaks && mkdir /source /evidence /work && chmod 1777 /work
WORKDIR /work
USER 10001:10001
ENTRYPOINT ["python", "/opt/runner/stage.py"]
