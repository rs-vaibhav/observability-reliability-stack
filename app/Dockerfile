FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY orders_service.py .
RUN useradd -r -u 10001 app && mkdir -p /var/log/app && chown 10001 /var/log/app
USER 10001
EXPOSE 8000
# uvicorn's --timeout-graceful-shutdown lets in-flight requests finish after SIGTERM
CMD ["uvicorn", "orders_service:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "20"]
