FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY indexer.py example_queries.py kg-config.env ./
COPY kg_indexer/ ./kg_indexer/

# Overridden by docker-compose.yml — kept here so `docker run` alone works
# against whatever is mounted at /workspace.
ENV ROOT_PATH=/workspace
ENV OUTPUT_DIR=/app/output

ENTRYPOINT ["python", "indexer.py"]
