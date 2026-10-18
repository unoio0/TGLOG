FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY logger.py .

VOLUME /app/data

CMD ["python", "-u", "logger.py"]
