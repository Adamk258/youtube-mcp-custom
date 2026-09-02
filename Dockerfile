# YouTube MCP — container for Render / Fly.io / Hugging Face Spaces / any PaaS.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .

ENV MCP_HOST=0.0.0.0
# Platforms inject PORT; server.py reads MCP_PORT || PORT || 8765.
EXPOSE 8765

CMD ["python", "server.py"]
