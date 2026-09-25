FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app

RUN uv pip install --system --no-cache "fastapi>=0.115.0" "uvicorn>=0.30.0"

ENV PORT=8100
EXPOSE 8100

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
