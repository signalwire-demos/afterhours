# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Wire Heating and Air - an after-hours HVAC emergency service agent with a SignalWire AI backend (Python/FastAPI) and vanilla JavaScript WebRTC frontend. Service requests are collected via phone calls and displayed on a web dashboard.

## Commands

```bash
# Local development (activate venv first or run in container)
python app.py

# Production (used by Procfile)
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --worker-class uvicorn.workers.UvicornWorker
```

## Architecture

**Backend (`app.py`):**
- Uses `signalwire-agents` SDK with `AgentBase` and `AgentServer` classes
- Multi-context workflow pattern with `define_contexts()`, `add_context()`, `add_step()`
- Contexts: `greeting` -> `service_request` -> `confirmation`
- SWAIG functions defined with `@self.tool()` decorator for each step
- Context switching via `.swml_change_context()`
- State persistence via `global_data` and `.update_global_data()`
- In-memory storage: `SERVICE_REQUESTS` dict

**Service Request Data Model:**
```python
{
    "id": "req_abc123",
    "customer_name": "John Smith",
    "service_address": "123 Main St, Austin TX 78701",
    "unit_info": "Carrier AC, ~10 years, rooftop",
    "ownership": "rent",              # "own" or "rent"
    "callback_primary": "+15551234567",
    "callback_alternate": "+15559876543",
    "issue_type": "ac_repair",        # "ac_repair" or "heating_repair"
    "is_emergency": True,
    "issue_description": "AC stopped working, house is 95 degrees",
    "created_at": "2025-01-10T22:30:00Z",
    "status": "pending"               # pending, dispatched, resolved
}
```

**Frontend (`web/`):**
- Static files served by AgentServer
- Displays service requests with emergency highlighting
- Real-time updates via `request_submitted` user events

**Key endpoints:**
- `GET /get_token` - Returns `{token, address}` for WebRTC client
- `GET /api/requests` - All service requests
- `GET /api/config` - Company name and phone number
- `GET /health` - Health check
- `POST /afterhours` - SWML endpoint (called by SignalWire)

## Environment Variables

Required:
- `SIGNALWIRE_SPACE_NAME` - Your SignalWire space
- `SIGNALWIRE_PROJECT_ID` - Project ID
- `SIGNALWIRE_TOKEN` - API token

URL detection (one required for SWML callbacks):
- `SWML_PROXY_URL_BASE` - Set for local dev with ngrok
- `APP_URL` - Auto-set on Dokku/Heroku

Optional:
- `AGENT_NAME` - Handler name (default: "afterhours")
- `PHONE_NUMBER` - Display on website
- `POST_PROMPT_URL` - Webhook for call summaries
- `SWML_BASIC_AUTH_USER/PASSWORD` - Secures SWML endpoint

## Key Patterns

**Context/Step Definition:**
```python
contexts = self.define_contexts()
greeting = contexts.add_context("greeting")
greeting.add_step("welcome") \
    .set_text("Thank you for calling Wire Heating and Air...") \
    .set_step_criteria("Customer indicates they need service") \
    .set_functions(["start_service_request"])
```

**SWAIG Function with Context Switch:**
```python
@self.tool(name="start_service_request", description="...")
def start_service_request(args, raw_data):
    return (
        SwaigFunctionResult("I'll get a service request started...")
        .swml_change_context("service_request")
        .update_global_data({"pending_request": {}})
    )
```

## Deployment

Configured for Dokku with `.dokku/` config files. Health checks via `/health` and `/ready` endpoints.
