FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && adduser --disabled-password --gecos '' splitbot \
    && chown -R splitbot:splitbot /app
COPY bot.py db.py ./
RUN mkdir -p data && chown splitbot:splitbot data
VOLUME ["/app/data"]
USER splitbot
ENV TELEGRAM_BOT_TOKEN=""
CMD ["python", "bot.py"]
