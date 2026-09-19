FROM python:3.11-slim
WORKDIR /app
COPY code/requirements.txt .
RUN pip install --no-cache-dir --timeout 300 --retries 10 -r requirements.txt
COPY code/ .
EXPOSE 9001
ENTRYPOINT ["uvicorn", "invoice_agent:app", "--host", "0.0.0.0", "--port", "9001"]