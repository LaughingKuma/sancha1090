FROM python:3.12-slim

WORKDIR /app
# Drop root: the service only reads RW and serves static files
RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static ./static
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Single worker: one in-process poller owns the shared snapshot cache
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
