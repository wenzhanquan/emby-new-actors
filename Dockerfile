FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai
LABEL org.opencontainers.image.source="https://github.com/wenzhanquan/emby-new-actors"
COPY refresh_new_actors.py scheduler.py initialize.py /app/
CMD ["python", "/app/scheduler.py"]
