FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml ./
COPY analysis ./analysis
COPY agent ./agent
COPY apps ./apps
COPY infrastructure ./infrastructure
COPY integrations ./integrations
COPY knowledge ./knowledge
COPY security ./security
COPY workflow ./workflow
COPY observability ./observability
COPY evaluation ./evaluation
RUN pip install ".[langgraph]" && useradd --create-home --uid 10001 agent
USER agent
EXPOSE 8762
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8762"]
