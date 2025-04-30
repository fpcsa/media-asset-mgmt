FROM python:3.11-slim

# Set workdir
WORKDIR /app

# Copy app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY video_transcoding_main_server.py .

# Expose port
EXPOSE 8004

# Command
CMD ["uvicorn", "video_transcoding_main_server:app", "--host", "0.0.0.0", "--port", "8004"]
