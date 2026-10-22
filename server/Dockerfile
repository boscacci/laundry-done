FROM python:3.13-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home relay && install -d -o relay /data
RUN python -c "import urllib.request,hashlib; p='/usr/local/bin/aws_signing_helper'; urllib.request.urlretrieve('https://rolesanywhere.amazonaws.com/releases/1.8.5/X86_64/Linux/Amzn2023/aws_signing_helper',p); assert hashlib.sha256(open(p,'rb').read()).hexdigest()=='beec9ed1c492d93db809890f16713e3556353294b823c2184ad4e891f1b2b54d'" && chmod 755 /usr/local/bin/aws_signing_helper
COPY src ./src
RUN find / -xdev -type f -perm /6000 -exec chmod a-s {} +
USER relay
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2)"]
CMD ["uvicorn", "laundry_done_relay.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers"]
