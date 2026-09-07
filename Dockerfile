FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY 2n2mqtt.py .

CMD ["python", "-u", "2n2mqtt.py"]
