# Wire Heating and Air

An after-hours emergency HVAC service agent built with SignalWire AI. Customers call to report heating and air conditioning emergencies, while staff view service requests through a real-time web dashboard.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                             │
│    Customer calls  ───►  AI Agent  ───►  Service Request Created            │
│                                                                             │
│                              │                                              │
│                              ▼                                              │
│                       Web Dashboard                                         │
│                    (real-time updates)                                      │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Features

- **After-Hours Service** - 24/7 emergency call handling for HVAC issues
- **Emergency Classification** - Two-tier urgency: emergency vs non-emergency
- **Complete Data Collection** - Name, address, unit info, ownership, callback numbers
- **Real-time Dashboard** - Live updates when service requests are submitted
- **Multi-context AI** - Guided conversation prevents errors
- **In-memory Storage** - Simple deployment, no database required

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              SIGNALWIRE                                 │
│  ┌─────────────┐         ┌─────────────┐         ┌─────────────┐        │
│  │   Phone     │         │   WebRTC    │         │    SWML     │        │
│  │   Network   │────────►│   Gateway   │────────►│   Handler   │        │
│  └─────────────┘         └─────────────┘         └──────┬──────┘        │
└─────────────────────────────────────────────────────────┼───────────────┘
                                                          │
                                                          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      WIRE HEATING AND AIR SERVER                        │
│                                                                         │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │                      AfterHoursAgent                               │ │
│  │  ┌──────────┐    ┌───────────┐    ┌──────────┐                     │ │
│  │  │ Greeting │───►│  Service  │───►│ Confirm  │                     │ │
│  │  │ Context  │    │  Request  │    │ Context  │                     │ │
│  │  └──────────┘    │  Context  │    └──────────┘                     │ │
│  │                  └───────────┘                                     │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                    │                                    │
│                                    ▼                                    │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                     SERVICE_REQUESTS (dict)                     │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                    │                                    │
│                                    ▼                                    │
│                            API Routes                                   │
│                         /api/requests                                   │
│                         /api/config                                     │
└─────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           WEB DASHBOARD                                 │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  ┌─────────┐  ┌───────────────────────────────────────────────┐  │   │
│  │  │  Video  │  │           Service Requests                    │  │   │
│  │  │  Call   │  │  ┌──────────────────────────────────────────┐ │  │   │
│  │  │         │  │  │ [EMERGENCY] AC Repair                    │ │  │   │
│  │  ├─────────┤  │  │  John Smith - 123 Main St                │ │  │   │
│  │  │ Connect │  │  │  AC not working, house at 95 degrees     │ │  │   │
│  │  └─────────┘  │  └──────────────────────────────────────────┘ │  │   │
│  │               │  ┌──────────────────────────────────────────┐ │  │   │
│  │  Activity Log │  │ Heating Repair                           │ │  │   │
│  │  ───────────  │  │  Jane Doe - 456 Oak Ave                  │ │  │   │
│  │  Connected... │  │  Furnace making noise                    │ │  │   │
│  │  New request..│  └──────────────────────────────────────────┘ │  │   │
│  └──────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
```

## Conversation Flow

```
                                    START
                                      │
                                      ▼
                        ┌─────────────────────────┐
                        │    GREETING CONTEXT     │
                        │                         │
                        │  "Thank you for calling │
                        │   Wire Heating and Air  │
                        │   after-hours service.  │
                        │   Are you experiencing  │
                        │   a heating or AC       │
                        │   problem?"             │
                        └───────────┬─────────────┘
                                    │
                                    ▼
                        ┌─────────────────────────┐
                        │  SERVICE REQUEST        │
                        │  CONTEXT                │
                        │                         │
                        │  Collect in order:      │
                        │  1. Issue type + urgent │
                        │  2. Customer name       │
                        │  3. Service address     │
                        │  4. Unit information    │
                        │  5. Own or rent         │
                        │  6. Callback number(s)  │
                        │  7. Issue description   │
                        └───────────┬─────────────┘
                                    │
                                    ▼
                        ┌─────────────────────────┐
                        │  CONFIRMATION CONTEXT   │
                        │                         │
                        │  "Let me confirm:       │
                        │   [Name] at [Address]   │
                        │   [Issue] - [Urgency]   │
                        │   Callback: [Phone]     │
                        │   Is this correct?"     │
                        │                         │
                        │  ┌─────────┐ ┌────────┐ │
                        │  │ Confirm │ │ Cancel │ │
                        │  └────┬────┘ └───┬────┘ │
                        └───────┼──────────┼──────┘
                                │          │
                                ▼          ▼
                        ┌──────────────┐  Back to
                        │   REQUEST    │  Greeting
                        │   SUBMITTED  │
                        │              │
                        │ Ticket ID    │
                        │ provided     │
                        │              │
                        │ Real-time    │
                        │ update sent  │
                        │ to dashboard │
                        └──────────────┘
```

## Data Collected

| Field | Description | Example |
|-------|-------------|---------|
| Issue Type | AC repair or heating repair | `ac_repair` |
| Is Emergency | True if urgent situation | `true` |
| Customer Name | Name of the customer | `John Smith` |
| Service Address | Full address with unit | `123 Main St, Apt 4B, Austin TX` |
| Unit Info | HVAC unit details | `Carrier AC, 10 years, rooftop` |
| Ownership | Own or rent | `rent` |
| Callback Primary | Main callback number | `+15551234567` |
| Callback Alternate | Backup number (optional) | `+15559876543` |
| Issue Description | Detailed problem description | `AC blowing warm air` |

## Quick Start

### Prerequisites

- Python 3.11+
- SignalWire account ([sign up free](https://signalwire.com))

### Installation

```bash
# Clone the repository
git clone https://github.com/signalwire-demos/afterhours.git
cd afterhours

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your SignalWire credentials
```

### Configuration

Edit `.env` with your settings:

```bash
# Required - SignalWire credentials
SIGNALWIRE_SPACE_NAME=your-space
SIGNALWIRE_PROJECT_ID=your-project-id
SIGNALWIRE_TOKEN=your-api-token

# Required for local dev - use ngrok or similar
SWML_PROXY_URL_BASE=https://your-ngrok-url.ngrok.io

# Optional - display phone number on website
PHONE_NUMBER=+1-555-123-4567

# Optional - post-call summary webhook
POST_PROMPT_URL=https://your-webhook.com/summary
```

### Running

```bash
# Local development
python app.py

# Production (via Procfile)
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --worker-class uvicorn.workers.UvicornWorker
```

Open http://localhost:5000 to view the dashboard.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/config` | GET | Returns company name and phone number |
| `/api/requests` | GET | All service requests |
| `/api/requests/{id}` | GET | Single service request details |
| `/get_token` | GET | WebRTC authentication token |
| `/health` | GET | Health check |
| `/afterhours` | POST | SWML webhook (called by SignalWire) |

### Example: Get Service Requests

```bash
curl http://localhost:5000/api/requests
```

```json
{
  "requests": [
    {
      "id": "req_a1b2c3d4",
      "customer_name": "John Smith",
      "service_address": "123 Main St, Austin TX 78701",
      "unit_info": "Carrier AC, 10 years old",
      "ownership": "own",
      "callback_primary": "+15551234567",
      "callback_alternate": "",
      "issue_type": "ac_repair",
      "is_emergency": true,
      "issue_description": "AC stopped working, house is 95 degrees",
      "created_at": "2025-01-10T22:30:00Z",
      "status": "pending"
    }
  ],
  "emergency_count": 1,
  "total_count": 1
}
```

## Data Model

```
┌─────────────────────────────────────────────────────────────────┐
│                      SERVICE REQUEST                            │
├─────────────────────────────────────────────────────────────────┤
│  id                   string      "req_a1b2c3d4"                │
│  customer_name        string      "John Smith"                  │
│  service_address      string      "123 Main St, Austin TX"      │
│  unit_info            string      "Carrier AC, 10 years"        │
│  ownership            string      "own" | "rent"                │
│  callback_primary     string      "+15551234567"                │
│  callback_alternate   string      "+15559876543"                │
│  issue_type           string      "ac_repair" | "heating_repair"│
│  is_emergency         boolean     true | false                  │
│  issue_description    string      "AC stopped working..."       │
│  created_at           string      "2025-01-10T22:30:00Z"        │
│  status               string      "pending" | "dispatched"      │
└─────────────────────────────────────────────────────────────────┘
```

## Tech Stack

- **Backend**: Python, FastAPI, SignalWire Agents SDK
- **Frontend**: Vanilla JavaScript, SignalWire WebRTC SDK
- **AI**: SignalWire AI with multi-context SWML
- **Deployment**: Dokku/Heroku compatible

## Project Structure

```
afterhours/
├── app.py              # Main application (agent + server)
├── web/
│   ├── index.html      # Dashboard UI
│   ├── app.js          # Frontend logic
│   └── styles.css      # Styling
├── .env.example        # Environment template
├── requirements.txt    # Python dependencies
├── Procfile           # Production server config
└── .dokku/            # Deployment configuration
```

## Deployment

The app is configured for Dokku/Heroku deployment:

1. Set environment variables on your platform
2. Push to deploy
3. The app auto-registers its SWML handler with SignalWire on startup

For local development with phone calls, use [ngrok](https://ngrok.com) to expose your local server:

```bash
ngrok http 5000
# Set SWML_PROXY_URL_BASE to the ngrok URL
```

## License

MIT
