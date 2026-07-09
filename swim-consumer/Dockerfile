FROM python:3.10-slim
# Task 1 confirms the solace-pubsubplus manylinux/glibc CPython wheel resolves on this base.

WORKDIR /app
RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY swim-consumer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY include ./include
COPY swim-consumer/consumer.py .
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# image-level probe (compose overrides with its own) so `docker run` outside compose is self-describing
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["python", "consumer.py"]
