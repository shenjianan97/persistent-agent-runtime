FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copy the full service source before installing the local package so package
# discovery sees the in-repo modules and entrypoint script.
COPY . .

RUN python -m pip install --upgrade pip && \
    pip install .

CMD ["python", "main.py"]
