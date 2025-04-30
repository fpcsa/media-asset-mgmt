FROM python:3.11-slim

# Set workdir
WORKDIR /app

# Copy and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vod_main_server.py .
COPY redis_adapter.py .

# Expose port
EXPOSE 8005

# Command
CMD ["uvicorn", "vod_main_server:app", "--host", "0.0.0.0", "--port", "8005"]
