FROM python:3.11-slim

# System libs for pymupdf / torch runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# bge-m3 model downloads to the HF cache on first use — mount a volume for it
# (see docker-compose.yaml) so it persists across restarts.
EXPOSE 8000
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
