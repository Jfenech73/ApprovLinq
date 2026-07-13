FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip

RUN apt-get update && apt-get install -y \
    libgomp1 \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
 && rm -rf /var/lib/apt/lists/*

COPY ApprovLinq/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY ApprovLinq /app

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && if [ \"${APPROVLINQ_START_SCAN_WORKER:-1}\" != \"0\" ]; then python scripts/scan_worker.py & fi && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
