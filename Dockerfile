FROM python:3.12-slim
ARG PINTXOS_VERSION=dev

WORKDIR /app
COPY pyproject.toml ./
RUN mkdir pintxos && touch pintxos/__init__.py \
    && pip install --no-cache-dir . \
    && rm -rf pintxos build *.egg-info
COPY pintxos ./pintxos
RUN pip install --no-cache-dir --no-deps .

RUN useradd --uid 1000 --create-home pintxos \
    && mkdir -p /data \
    && chown -R pintxos:pintxos /data

USER pintxos
ENV PINTXOS_DATA_DIR=/data
ENV PINTXOS_HOST=0.0.0.0
ENV PINTXOS_VERSION=$PINTXOS_VERSION
VOLUME /data
EXPOSE 8000
CMD ["python", "-m", "pintxos"]
