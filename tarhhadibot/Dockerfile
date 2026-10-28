FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home bot
COPY schoolbot ./schoolbot
COPY tarhhadibot.py .
USER bot
CMD ["python", "tarhhadibot.py"]
