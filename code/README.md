# Agentic AP Automation (Phase C1)

LangGraph state machine that orchestrates SAP Document AI extraction, LLM validation via AI Core, and S/4HANA supplier invoice posting. Serves as a REST endpoint deployable on SAP AI Core.

## Architecture

```
POST /v1/invoke {pdf_base64, pdf_filename}
    |
    v
+---------+     +----------+     +-------+
| extract | --> | validate | --> | route |
+---------+     +----------+     +---+---+
                                     |
              green  yellow  red     |
              /       |       \     |
             v        v        v    |
         +------+ +--------+ +--------+
         | post | |approve | | reject |
         +------+ +--------+ +--------+
              \       |       /
               \      v      /
                \  +-------+
                 -| audit |
                  +-------+
                     |
                     v
                    END
```

## Files

| File | Purpose |
|------|---------|
| `invoice_agent.py` | LangGraph nodes + FastAPI wrapper + local CLI entry |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Container image build |
| `serving-template.yaml` | SAP AI Core deployment spec |

## Local test

```bash
# Install deps
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Set env vars (or export from a .env file)
export DOC_AI_URL=https://<region>.doc.cloud.sap
export DOC_AI_TOKEN_URL=https://<subaccount>.authentication.<region>.hana.ondemand.com/oauth/token
export DOC_AI_CLIENT_ID=sb-xxxxx
export DOC_AI_CLIENT_SECRET=xxxxx
export AI_CORE_URL=https://api.ai.<region>.hana.ondemand.com/v2/inference/deployments/<id>
export AI_CORE_TOKEN=<bearer>
export S4_ODATA_URL=https://<host>/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV
export S4_USER=<user>
export S4_PASSWORD=<pwd>

# Run against a local PDF
python invoice_agent.py /path/to/sample_invoice.pdf

# Or serve as REST
uvicorn invoice_agent:app --host 0.0.0.0 --port 8080
curl -X POST http://localhost:8080/v1/invoke \
  -H "Content-Type: application/json" \
  -d "{\"pdf_filename\":\"inv.pdf\",\"pdf_base64\":\"$(base64 -w0 sample.pdf)\"}"
```

## Container build

```bash
cd deploy
cp ../code/invoice_agent.py .
cp ../code/requirements.txt .
docker build -t agentic-ap-automation:1.0.0 .
docker tag agentic-ap-automation:1.0.0 <your-dockerhub-user>/agentic-ap-automation:1.0.0
docker push <your-dockerhub-user>/agentic-ap-automation:1.0.0
```

## Deploy on SAP AI Core

Full step by step is in the Architecture and Deployment Guide docx.

Short version:

1. Push image to Docker Hub or SAP BTP container registry
2. Create AI Core resource group (or reuse existing)
3. Register Docker registry secret in AI Core
4. Create Generic Secrets: `document-ai-secret`, `ai-core-llm-secret`, `s4-hana-secret`, `sbpa-secret`
5. Register Git repository containing this `serving-template.yaml`
6. Create application in AI Core pointing at the repo
7. Create configuration referencing the executable
8. Create deployment; wait for status = RUNNING
9. Get inference URL from deployment details
10. Test with a real invoice PDF

## Confidence thresholds (tune per client)

| Range | Route | Action |
|-------|-------|--------|
| >= 80% | green | Auto post to S/4HANA |
| 51% to 80% | yellow | Route to SBPA approver task |
| < 51% | red | Notify AP team, no auto action |

## Next (Phase C2)

- SBPA workflow (email trigger, agent call, approval task, S/4 post fallback)
- End to end test with real vendor invoice
- Cloud ALM monitoring integration
- Confidence threshold tuning based on live data
