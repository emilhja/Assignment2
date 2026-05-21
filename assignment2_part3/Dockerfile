FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/assignment2_part3:/app/assignment2_part2

WORKDIR /app

COPY assignment2_part2/requirements.txt /app/assignment2_part2/requirements.txt
COPY assignment2_part3/requirements.txt /app/assignment2_part3/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/assignment2_part3/requirements.txt

RUN useradd --create-home --shell /bin/bash agentuser

COPY --chown=agentuser:agentuser assignment2_part2/ /app/assignment2_part2/
COPY --chown=agentuser:agentuser assignment2_part3/ /app/assignment2_part3/
RUN mkdir -p /workspace && chown -R agentuser:agentuser /app /workspace

USER agentuser
WORKDIR /app/assignment2_part3

CMD ["python", "agent.py"]
