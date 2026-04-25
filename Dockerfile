FROM python:3.11-slim

ENV PYTHONUTF8=1
ENV PYTHONIOENCODING=UTF-8

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-X", "utf8", "vpn_approval_bot/bot.py"]
