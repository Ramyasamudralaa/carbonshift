# CarbonShift demo worker.
#
# Build and run standalone before wiring it into AWS:
#   docker build -t carbonshift-worker src/worker
#   docker run --rm -e CARBONSHIFT_JOB_PAYLOAD=local-test carbonshift-worker
#
# The workload uses only the standard library, so there is nothing to install.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WORKER_RUN_SECONDS=5

WORKDIR /app

COPY job.py /app/job.py

# Run as a non-root user; Fargate does not require root for this workload.
RUN useradd --create-home --uid 10001 carbonshift
USER carbonshift

ENTRYPOINT ["python", "/app/job.py"]
