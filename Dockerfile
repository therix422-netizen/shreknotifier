FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server_hopper_backend.py .

CMD ["python", "server_hopper_backend.py"]
