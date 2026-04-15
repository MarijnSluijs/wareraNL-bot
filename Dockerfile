FROM python:3.12.9-slim-bookworm

WORKDIR /bot

# Install dependencies first (cached layer)
COPY pyproject.toml .
RUN pip install --no-cache-dir ".[website]"

COPY . .

# CMD instead of ENTRYPOINT so docker-compose can override per service
CMD ["python", "bot.py"]