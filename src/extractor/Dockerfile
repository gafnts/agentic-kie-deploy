FROM public.ecr.aws/lambda/python:3.13

COPY --from=ghcr.io/astral-sh/uv@sha256:2381d6aa60c326b71fd40023f921a0a3b8f91b14d5db6b90402e65a635053709 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1

COPY pyproject.toml uv.lock ${LAMBDA_TASK_ROOT}/
RUN uv sync --locked --system --no-dev --no-install-project

COPY handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.handler"]
