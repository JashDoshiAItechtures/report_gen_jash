FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (if needed, extend this)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

ENV PYTHONUNBUFFERED=1

# Hugging Face Spaces pass the port via the PORT env var
ENV PORT=7860
EXPOSE 7860

# Start FastAPI app with uvicorn
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
