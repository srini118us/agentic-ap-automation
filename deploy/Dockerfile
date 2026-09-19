# Multi stage build for the Agentic AP Automation
# Target: SAP AI Core serving runtime

FROM python:3.11-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --target=/deps -r requirements.txt

FROM python:3.11-slim
LABEL maintainer="Srinivasa"
LABEL description="LangGraph invoice processing agent for SAP AI Core"

# Non root user (SAP AI Core requirement)
RUN useradd -m -u 1000 aicore
WORKDIR /app

COPY --from=builder /deps /usr/local/lib/python3.11/site-packages
COPY invoice_agent.py /app/

USER aicore
EXPOSE 8080

# AI Core standard entry point
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "invoice_agent:app", "--host", "0.0.0.0", "--port", "8080"]
