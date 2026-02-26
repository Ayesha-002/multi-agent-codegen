FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py .
COPY utils.py .
COPY sanitizer.py .

CMD ["python", "-u", "agent.py"]
