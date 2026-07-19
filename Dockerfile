FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY boss_cli ./boss_cli
RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["boss-plugin"]