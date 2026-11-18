FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py panel.html sms_pool.py ./
COPY config.example.json ./

RUN mkdir -p logs && cp config.example.json config.json

EXPOSE 4556
VOLUME ["/app/logs"]

# 容器内监听 0.0.0.0 时请在 config.json 里改 listen.host，并通过 nginx/反代加鉴权
CMD ["python", "app.py"]
