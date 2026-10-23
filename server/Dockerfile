FROM python:3.13-alpine3.24@sha256:bf12ecc0bea3f171d55145dd02c78b5eeb09f30ee2ac9cc2b4c9e4e8f5e30741
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
COPY requirements.txt .
RUN apk add --no-cache gcompat=1.1.0-r4 \
    && pip install --no-cache-dir --only-binary=:all: -r requirements.txt \
    && addgroup -g 10001 relay && adduser -D -u 10001 -G relay relay \
    && mkdir /data && chown relay:relay /data
RUN python -c "import urllib.request,hashlib; p='/usr/local/bin/aws_signing_helper'; urllib.request.urlretrieve('https://rolesanywhere.amazonaws.com/releases/1.8.5/X86_64/Linux/Amzn2023/aws_signing_helper',p); assert hashlib.sha256(open(p,'rb').read()).hexdigest()=='beec9ed1c492d93db809890f16713e3556353294b823c2184ad4e891f1b2b54d'" && chmod 755 /usr/local/bin/aws_signing_helper
COPY src ./src
RUN find / -xdev -type f -perm /6000 -exec chmod a-s {} +
USER relay
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2)"]
CMD ["uvicorn", "laundry_done_relay.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers"]
