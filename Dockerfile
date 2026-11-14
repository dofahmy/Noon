FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt ./
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt && python -m playwright install --with-deps chromium
COPY . .
CMD ["python", "noon_forwarder.py"]
RUN echo "NOON-V18-FRESH-BUILD-20261114"
