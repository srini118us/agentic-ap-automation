# Agentic AP Automation

LangGraph based agent for supplier invoice processing on SAP BTP.

## Workflow

![LangGraph Workflow](docs/workflow.png)

7 nodes with conditional routing:
- **extract**: SAP Document AI (headers + line items)
- **validate**: LLM anomaly detection + confidence score
- **route**: green (>=0.80) | yellow (0.51-0.80) | red (<0.51)
- **post**: S/4HANA OData (mocked; production via SBPA HTTP Action)
- **approve**: SBPA task for human review
- **reject**: AP team notification
- **audit**: structured log for all paths

## Architecture

- **SAP Document AI**: PDF extraction (34 formats supported)
- **SAP AI Core**: LLM validation via gpt-4o-mini deployment
- **LangGraph**: agent orchestration and state management
- **SBPA**: production workflow for approval + posting

## Run

`ash
python invoice_agent.py <path_to_invoice.pdf>
`

## Environment

Requires `.env` with credentials for:
- SAP Document AI (EU10)
- SAP AI Core (US10)
- S/4HANA (on premise)
- SBPA (optional, for Phase C2)

Sample template in `.env.example`.

## Generate workflow diagram

`ash
python invoice_agent.py --graph-png
`

Requires `pyppeteer` (`pip install pyppeteer`).
