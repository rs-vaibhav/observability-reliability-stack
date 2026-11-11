FROM python:3.12-slim
RUN pip install --no-cache-dir httpx==0.28.0
COPY loadgen.py /loadgen.py
USER 65534
ENTRYPOINT ["python", "/loadgen.py"]
