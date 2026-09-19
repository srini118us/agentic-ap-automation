"""
Invoice Processing Agent
LangGraph state machine orchestrating Document AI, LLM validation, and S/4HANA posting.

Nodes:
  extract  -> call SAP Document AI, get structured JSON
  validate -> call LLM (via AI Core) to cross check fields, flag anomalies
  route    -> confidence based branching (green/yellow/red)
  post     -> S/4HANA OData supplier invoice creation (green path)
  approve  -> queue for SBPA approval (yellow path)
  reject   -> notify AP team (red path)
  audit    -> write outcome to audit log

Entry: POST /invoke with {"pdf_url": "...", "pdf_base64": "..."}
Exit:  JSON with status, s4_posting_doc, confidence_summary
"""

from __future__ import annotations
import os
import json
from dotenv import load_dotenv
load_dotenv()
import base64
import logging
from typing import TypedDict, Literal, Optional
from datetime import datetime

import requests
from langgraph.graph import StateGraph, END

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("invoice_agent")

# =========================================================================
# Configuration (from env vars, injected via AI Core serving template)
# =========================================================================
DOC_AI_URL       = os.environ["DOC_AI_URL"]              # https://<region>.doc.cloud.sap
DOC_AI_TOKEN_URL = os.environ["DOC_AI_TOKEN_URL"]        # OAuth token endpoint
DOC_AI_CLIENT_ID = os.environ["DOC_AI_CLIENT_ID"]
DOC_AI_SECRET    = os.environ["DOC_AI_CLIENT_SECRET"]
DOC_AI_CLIENT    = os.environ.get("DOC_AI_CLIENT", "default")

AI_CORE_URL       = os.environ["AI_CORE_URL"]             # LLM inference endpoint
AI_CORE_AUTH_URL  = os.environ["AI_CORE_AUTH_URL"]        # OAuth2 token endpoint
AI_CORE_CLIENT_ID = os.environ["AI_CORE_CLIENT_ID"]
AI_CORE_SECRET    = os.environ["AI_CORE_CLIENT_SECRET"]
AI_CORE_MODEL     = os.environ.get("AI_CORE_MODEL", "gpt-4o-mini")

S4_ODATA_URL     = os.environ["S4_ODATA_URL"]            # https://<host>/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV
S4_USER          = os.environ["S4_USER"]                 # basic auth for POC (principal propagation in prod)
S4_PASSWORD      = os.environ["S4_PASSWORD"]

SBPA_APPROVAL_URL = os.environ.get("SBPA_APPROVAL_URL", "")  # webhook to SBPA for yellow path
NOTIFY_EMAIL      = os.environ.get("NOTIFY_EMAIL", "ap-team@company.com")

CONF_GREEN  = 0.80
CONF_YELLOW = 0.51

# =========================================================================
# State schema
# =========================================================================
class AgentState(TypedDict, total=False):
    pdf_bytes:          bytes
    pdf_filename:       str
    document_id:        str
    extraction:         dict
    validation:         dict
    confidence_avg:     float
    route:              Literal["green", "yellow", "red"]
    s4_posting_doc:     Optional[str]
    approval_task_id:   Optional[str]
    audit_id:           Optional[str]
    error:              Optional[str]

# =========================================================================
# Helper: OAuth token for Document AI
# =========================================================================
def _get_doc_ai_token() -> str:
    r = requests.post(
        DOC_AI_TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(DOC_AI_CLIENT_ID, DOC_AI_SECRET),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]

def _get_ai_core_token() -> str:
    r = requests.post(
        AI_CORE_AUTH_URL,
        data={"grant_type": "client_credentials"},
        auth=(AI_CORE_CLIENT_ID, AI_CORE_SECRET),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]

# =========================================================================
# Node 1: extract via Document AI
# =========================================================================
def node_extract(state: AgentState) -> AgentState:
    log.info("[extract] uploading %s", state.get("pdf_filename"))
    token = _get_doc_ai_token()
    headers = {"Authorization": f"Bearer {token}"}

    options = {
        "clientId":     DOC_AI_CLIENT,
        "documentType": "invoice",
        "schemaName":   "SAP_invoice_schema",
    }
    files = {
        "file":    (state["pdf_filename"], state["pdf_bytes"], "application/pdf"),
        "options": (None, json.dumps(options), "application/json"),
    }
    r = requests.post(f"{DOC_AI_URL}/document-information-extraction/v1/document/jobs",
                      headers=headers, files=files, timeout=120)
    r.raise_for_status()
    job = r.json()
    doc_id = job["id"]
    log.info("[extract] job id=%s, polling for result", doc_id)

    # Poll for completion (production: use webhook, this is POC simplicity)
    import time
    for _ in range(60):
        time.sleep(3)
        rr = requests.get(f"{DOC_AI_URL}/document-information-extraction/v1/document/jobs/{doc_id}",
                          headers={"Authorization": f"Bearer {token}"}, timeout=30)
        rr.raise_for_status()
        payload = rr.json()
        if payload.get("status") in ("DONE", "FAILED"):
            break
    if payload.get("status") != "DONE":
        state["error"] = f"Document AI extraction failed: {payload.get('status')}"
        return state

    state["document_id"] = doc_id
    state["extraction"] = payload["extraction"]
    log.info("[extract] done, %d header fields, %d line items",
             len(payload["extraction"].get("headerFields", [])),
             len(payload["extraction"].get("lineItems", [])))
    return state

# =========================================================================
# Node 2: validate via LLM
# =========================================================================
def node_validate(state: AgentState) -> AgentState:
    if state.get("error"):
        return state
    log.info("[validate] cross checking fields via LLM")
    ex = state["extraction"]
    header = {f["name"]: f["value"] for f in ex.get("headerFields", []) if f["value"] != "None"}
    line_count = len(ex.get("lineItems", []))

    prompt = f"""You are an accounts payable auditor. Review these extracted invoice fields
and flag anomalies. Return JSON with keys: valid (bool), issues (list of strings),
confidence_avg (float 0..1).

Fields: {json.dumps(header, indent=2)}
Line items count: {line_count}

Checks:
1. Do sum of line items align with grossAmount? (if extractable)
2. Are tax IDs plausible format for the country codes?
3. Is due date after document date?
4. Is currency code valid ISO 4217?
5. Are IBANs (senderBankAccount) properly formatted?

Return ONLY the JSON, no prose.
"""
    ai_token = _get_ai_core_token()
    r = requests.post(
        f"{AI_CORE_URL}/chat/completions?api-version=2024-08-01-preview",
        headers={
            "Authorization": f"Bearer {ai_token}",
            "Content-Type": "application/json",
            "AI-Resource-Group": os.environ.get("AI_RESOURCE_GROUP", "default"),
        },
        json={
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 800,
            "temperature": 0.0,
        },
        timeout=60,
    )
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"].strip()
    # Strip potential markdown fences
    if text.startswith("```"):
        text = text.split("```")[1].lstrip("json").strip()
    try:
        v = json.loads(text)
    except json.JSONDecodeError:
        v = {"valid": False, "issues": ["LLM returned unparseable JSON"], "confidence_avg": 0.4}
    state["validation"] = v
    state["confidence_avg"] = float(v.get("confidence_avg", 0.5))
    log.info("[validate] confidence=%.2f, issues=%d", state["confidence_avg"], len(v.get("issues", [])))
    return state

# =========================================================================
# Node 3: route based on confidence
# =========================================================================
def node_route(state: AgentState) -> AgentState:
    if state.get("error"):
        state["route"] = "red"
        return state
    c = state.get("confidence_avg", 0.0)
    if c >= CONF_GREEN:
        state["route"] = "green"
    elif c >= CONF_YELLOW:
        state["route"] = "yellow"
    else:
        state["route"] = "red"
    log.info("[route] decision=%s (confidence=%.2f)", state["route"], c)
    return state

def route_condition(state: AgentState) -> str:
    return state["route"]

# =========================================================================
# Node 4a: post to S/4HANA (green path)
# =========================================================================
def _to_odata_date(iso_date: str) -> str:
    """Convert 'YYYY-MM-DD' to '/Date(<ms>)/' format required by S/4HANA OData."""
    if not iso_date or iso_date == "None":
        return None
    from datetime import datetime, timezone
    dt = datetime.strptime(iso_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)
    return f"/Date({ms})/"
def node_post(state: AgentState) -> AgentState:
    log.info("[post] MOCK: would POST to S/4HANA API_SUPPLIERINVOICE_PROCESS_SRV")
    log.info("[post] Production path: SBPA HTTP Action handles OData field mapping")
    state["s4_posting_doc"] = f"MOCK-INV-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    log.info("[post] mock document id=%s", state["s4_posting_doc"])
    return state

def _line_field(item, name):
    for f in item:
        if f["name"] == name:
            v = f["value"]
            return None if v == "None" else v
    return None

# =========================================================================
# Node 4b: send to SBPA approval (yellow path)
# =========================================================================
def node_approve(state: AgentState) -> AgentState:
    log.info("[approve] queuing SBPA approval task")
    if not SBPA_APPROVAL_URL:
        state["approval_task_id"] = "MOCK-APPROVAL-001"
        log.warning("SBPA_APPROVAL_URL not set, using mock task id")
        return state
    r = requests.post(
        SBPA_APPROVAL_URL,
        json={
            "document_id":    state.get("document_id"),
            "extraction":     state.get("extraction"),
            "validation":     state.get("validation"),
            "confidence_avg": state.get("confidence_avg"),
        },
        timeout=30,
    )
    r.raise_for_status()
    state["approval_task_id"] = r.json().get("taskId")
    return state

# =========================================================================
# Node 4c: reject / notify AP team (red path)
# =========================================================================
def node_reject(state: AgentState) -> AgentState:
    log.info("[reject] notifying AP team at %s", NOTIFY_EMAIL)
    # In production: call an email service or ticket API
    return state

# =========================================================================
# Node 5: audit log
# =========================================================================
def node_audit(state: AgentState) -> AgentState:
    audit_record = {
        "timestamp":       datetime.utcnow().isoformat() + "Z",
        "document_id":     state.get("document_id"),
        "pdf_filename":    state.get("pdf_filename"),
        "route":           state.get("route"),
        "confidence_avg":  state.get("confidence_avg"),
        "s4_posting_doc":  state.get("s4_posting_doc"),
        "approval_task":   state.get("approval_task_id"),
        "error":           state.get("error"),
    }
    log.info("[audit] %s", json.dumps(audit_record))
    # In production: write to Cloud Logging service or SAP audit table
    state["audit_id"] = f"AUD-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    return state

# =========================================================================
# Build the graph
# =========================================================================
def build_graph():
    g = StateGraph(AgentState)

    g.add_node("extract",  node_extract)
    g.add_node("validate", node_validate)
    g.add_node("route",    node_route)
    g.add_node("post",     node_post)
    g.add_node("approve",  node_approve)
    g.add_node("reject",   node_reject)
    g.add_node("audit",    node_audit)

    g.set_entry_point("extract")
    g.add_edge("extract", "validate")
    g.add_edge("validate", "route")

    g.add_conditional_edges(
        "route",
        route_condition,
        {"green": "post", "yellow": "approve", "red": "reject"},
    )

    g.add_edge("post",    "audit")
    g.add_edge("approve", "audit")
    g.add_edge("reject",  "audit")
    g.add_edge("audit",   END)

    return g.compile()

# =========================================================================
# FastAPI wrapper for AI Core serving
# =========================================================================
try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel

    app = FastAPI(title="Agentic AP Automation")
    _graph = build_graph()

    class InvokeRequest(BaseModel):
        pdf_base64: str
        pdf_filename: str = "invoice.pdf"

    @app.post("/v1/invoke")
    def invoke(req: InvokeRequest):
        try:
            initial: AgentState = {
                "pdf_bytes":    base64.b64decode(req.pdf_base64),
                "pdf_filename": req.pdf_filename,
            }
            final = _graph.invoke(initial)
            return {
                "status":         "error" if final.get("error") else "ok",
                "route":          final.get("route"),
                "confidence_avg": final.get("confidence_avg"),
                "s4_posting_doc": final.get("s4_posting_doc"),
                "approval_task_id": final.get("approval_task_id"),
                "audit_id":       final.get("audit_id"),
                "error":          final.get("error"),
            }
        except Exception as e:
            log.exception("agent invocation failed")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/v1/health")
    def health():
        return {"status": "healthy"}
except ImportError:
    log.warning("FastAPI not installed; running as library only")

# =========================================================================
# Local test entry (python invoice_agent.py path/to/invoice.pdf)
# =========================================================================
if __name__ == "__main__":
    import sys
    
    # Graph visualization
    if len(sys.argv) > 1 and sys.argv[1] == "--graph-png":
        graph = build_graph()
        png = graph.get_graph().draw_mermaid_png()
        with open("workflow.png", "wb") as f:
            f.write(png)
        print("Saved workflow.png")
        sys.exit(0)
    
    if len(sys.argv) > 1 and sys.argv[1] == "--graph":
        graph = build_graph()
        print(graph.get_graph().draw_mermaid())
        sys.exit(0)
    
    if len(sys.argv) < 2:
        print("Usage: python invoice_agent.py <pdf_file>")
        sys.exit(1)
    with open(sys.argv[1], "rb") as f:
        pdf_bytes = f.read()
    graph = build_graph()
    result = graph.invoke({
        "pdf_bytes":    pdf_bytes,
        "pdf_filename": os.path.basename(sys.argv[1]),
    })
    print(json.dumps({
        k: v for k, v in result.items() if k != "pdf_bytes" and k != "extraction"
    }, indent=2, default=str))