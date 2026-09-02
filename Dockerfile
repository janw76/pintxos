FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY pintxos ./pintxos
RUN pip install --no-cache-dir .

RUN useradd --uid 1000 --create-home pintxos \
    && mkdir -p /data \
    && chown -R pintxos:pintxos /data

USER pintxos
ENV PINTXOS_DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD ["python", "-m", "pintxos"]
