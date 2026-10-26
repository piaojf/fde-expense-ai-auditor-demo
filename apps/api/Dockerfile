FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY apps/api ./apps/api
COPY agent ./agent
COPY domain ./domain
COPY rules ./rules
COPY rag ./rag
COPY integrations ./integrations
COPY database ./database
COPY evals ./evals
COPY alembic.ini ./
RUN useradd --create-home auditor && chown -R auditor:auditor /app
USER auditor
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

