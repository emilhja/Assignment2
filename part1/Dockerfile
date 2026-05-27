FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --shell /bin/bash agentuser

COPY --chown=agentuser:agentuser . .
RUN mkdir -p /workspace && chown -R agentuser:agentuser /app /workspace

USER agentuser

CMD ["python", "agent.py"]
