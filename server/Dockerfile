FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV LAUNDRY_RELAY_FROM_ENV=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
ENV PYTHONPATH=/app/src

EXPOSE 8080
CMD ["uvicorn", "laundry_done_relay.main:app", "--host", "0.0.0.0", "--port", "8080"]
