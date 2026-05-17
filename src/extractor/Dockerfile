FROM public.ecr.aws/lambda/python:3.13

# Patch base-image OS packages (the Lambda base image lags AL2023 security
# releases; without this the Trivy gate fails on freshly-disclosed CVEs).
RUN dnf -y update && dnf clean all && rm -rf /var/cache/dnf

COPY --from=ghcr.io/astral-sh/uv@sha256:2381d6aa60c326b71fd40023f921a0a3b8f91b14d5db6b90402e65a635053709 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1
ENV UV_PROJECT_ENVIRONMENT=/var/lang

COPY pyproject.toml uv.lock ${LAMBDA_TASK_ROOT}/
RUN uv sync --locked --no-dev --no-install-project --inexact

COPY handler.py schema.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.handler"]
