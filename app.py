"""
===============================================================================
Wire Heating and Air - After-Hours Emergency Service Agent
===============================================================================

A SignalWire AI agent for handling after-hours HVAC emergency calls,
with a web dashboard to view service requests.

Features:
- Multi-context conversation flow for guided service request collection
- Emergency vs non-emergency classification
- In-memory service request storage
- Web API for viewing requests
- Real-time updates to frontend via user events

Usage:
    python app.py                    # Run locally
    gunicorn app:app ...            # Run in production (see Procfile)

Environment Variables (see .env.example):
    SIGNALWIRE_SPACE_NAME           # Required: Your SignalWire space
    SIGNALWIRE_PROJECT_ID           # Required: Your project ID
    SIGNALWIRE_TOKEN                # Required: Your API token
    SWML_PROXY_URL_BASE or APP_URL  # Auto-detected on Dokku/Heroku, set for local

===============================================================================
"""

import os
import time
import logging
import re
import json
import threading
import warnings
import random
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# -------------------------------------------------------------------------------
# SignalWire SDK imports
# -------------------------------------------------------------------------------
from signalwire import AgentBase, AgentServer
from signalwire.core.function_result import SwaigFunctionResult
from signalwire.rest import RestClient
from fastapi.responses import JSONResponse

# Load environment variables from .env file (for local development)
load_dotenv()

# -------------------------------------------------------------------------------
# Logging Configuration
# -------------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------------------
# Global State
# -------------------------------------------------------------------------------
swml_handler_info = {
    "id": None,
    "address_id": None,
    "address": None
}
# Reason the last handler-setup attempt didn't complete (surfaced via /get_token)
swml_setup_error = None
# Serializes the lazy /get_token re-registration so workers don't race
_swml_setup_lock = threading.Lock()

# Voice store file path (shared between workers)
VOICE_STORE_FILE = "/tmp/afterhours_voice.txt"

def get_stored_voice():
    """Get voice from shared file store."""
    try:
        with open(VOICE_STORE_FILE, 'r') as f:
            return f.read().strip()
    except:
        return None

def set_stored_voice(voice):
    """Set voice in shared file store."""
    with open(VOICE_STORE_FILE, 'w') as f:
        f.write(voice)

# -------------------------------------------------------------------------------
# Service Request Data Structures (In-Memory)
# -------------------------------------------------------------------------------
# Service requests are written by SWAIG handlers and read by the admin API.
# Gunicorn runs multiple workers, so a plain dict gave each worker its own copy
# and roughly half of all admin reads missed the ticket. File-backed + locked so
# every worker sees the same data. Set SERVICE_REQUEST_STORE to a mounted path
# to survive container recreation.
SERVICE_REQUEST_STORE = os.environ.get("SERVICE_REQUEST_STORE", "/tmp/afterhours_requests.json")
_STORE_LOCK = threading.Lock()


def _load_requests() -> dict:
    """Read every service request. Returns {} if the store is absent/corrupt."""
    try:
        with open(SERVICE_REQUEST_STORE, "r") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        print(f"[store] read failed: {e}", flush=True)
        return {}


# -------------------------------------------------------------------------------
# Build identity
# -------------------------------------------------------------------------------
# Mirrors the cinebot footer: every demo should be able to say which commit it is
# actually running. Resolution order matters - Dokku sets GIT_REV at runtime, the
# COMMIT file is written at image build (.git is excluded from the build context,
# so the container cannot shell out to git).
_COMMIT_CACHE = None


def resolve_commit() -> dict:
    """Return {commit, short, source, repo} for whatever this process is running."""
    global _COMMIT_CACHE
    if _COMMIT_CACHE is not None:
        return _COMMIT_CACHE

    here = Path(__file__).parent
    found, source = "", "unknown"

    for var in ("SOURCE_VERSION", "GIT_COMMIT", "COMMIT_SHA", "GIT_REV"):
        value = (os.environ.get(var) or "").strip()
        if value:
            found, source = value, f"env:{var}"
            break

    if not found:
        try:
            found = (here / "COMMIT").read_text(encoding="utf-8").strip()
            source = "file:COMMIT"
        except OSError:
            pass

    # Accept only something that looks like a SHA, so a stray file or a mis-set
    # variable cannot put arbitrary text into the page footer.
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", found or ""):
        found, source = "", "unknown"

    _COMMIT_CACHE = {
        "commit": found,
        "short": found[:7],
        "source": source,
        "repo": "https://github.com/signalwire-demos/afterhours",
    }
    return _COMMIT_CACHE


def _page_technician(record: dict) -> bool:
    """Notify the on-call technician about an emergency.

    Sends an SMS when TECH_PAGER_NUMBER and SIGNALWIRE_FROM_NUMBER are both
    configured; otherwise logs loudly so the demo still shows the branch firing.
    Returns True if a page was actually sent.
    """
    # Gas is the one detail a technician must see before they travel, and it is
    # only reliably in the gas_smell flag - callers do not always repeat it in
    # the free-text description.
    system = {"ac_repair": "A/C", "heating_repair": "Heating"}.get(
        record.get("issue_type", ""), record.get("issue_type", "Unknown")
    )
    when = datetime.utcnow().strftime("%H:%M UTC")

    desc = (record.get("issue_description") or "").strip()
    if len(desc) > 140:
        desc = desc[:140].rsplit(" ", 1)[0] + "..."

    callback = record.get("callback_primary", "")
    alt = record.get("callback_alternate", "")
    if alt:
        callback = f"{callback} / alt {alt}"

    lines = [f"EMERGENCY {record['id']} ({when})"]
    if record.get("gas_smell"):
        lines.append("** GAS SMELL REPORTED - caller advised to evacuate **")
    lines += [
        record.get("customer_name", "Unknown caller"),
        record.get("service_address", "No address given"),
        f"{system}. Callback {callback}",
    ]
    if desc:
        lines.append(desc)
    summary = "\n".join(lines)
    to_number = os.environ.get("TECH_PAGER_NUMBER")
    from_number = os.environ.get("SIGNALWIRE_FROM_NUMBER")
    if not (to_number and from_number):
        print(f"[page] (not configured, would send) {summary}", flush=True)
        return False
    try:
        client = build_rest_client()
        client.messages.create(from_=from_number, to=to_number, body=summary)
        print(f"[page] sent to {to_number}: {record['id']}", flush=True)
        return True
    except Exception as e:
        print(f"[page] FAILED for {record['id']}: {e}", flush=True)
        return False


def _save_request(ticket_number: str, record: dict) -> None:
    """Add one request, serialized across workers and written atomically."""
    with _STORE_LOCK:
        current = _load_requests()
        current[ticket_number] = record
        tmp = f"{SERVICE_REQUEST_STORE}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(current, fh)
        os.replace(tmp, SERVICE_REQUEST_STORE)


def generate_ticket_number():
    """Generate a unique 6-digit ticket number."""
    return str(random.randint(100000, 999999))


def say_digits(number_str: str) -> str:
    """Convert a number string to spoken words for TTS.

    Example: "123456" -> "one two three four five six"
    """
    digit_words = {
        '0': 'zero', '1': 'one', '2': 'two', '3': 'three', '4': 'four',
        '5': 'five', '6': 'six', '7': 'seven', '8': 'eight', '9': 'nine'
    }
    return ' '.join(digit_words.get(d, d) for d in number_str)

def say_phone(number_str: str) -> str:
    """Speak a phone number digit by digit.

    Echoing "+15555555555" straight into TTS reads it as a single enormous
    number. Strips formatting and reads the digits, same idea as say_digits
    but tolerant of +, spaces, dashes and parentheses.
    """
    digits = re.sub(r"\D", "", number_str or "")
    return say_digits(digits) if digits else (number_str or "")



# Server configuration
HOST = "0.0.0.0"
PORT = int(os.environ.get('PORT', 5000))


# ===============================================================================
# SWML Handler Registration Functions
# ===============================================================================

def get_signalwire_host():
    """Get the full SignalWire API host from the space name."""
    space = os.getenv("SIGNALWIRE_SPACE_NAME", "")
    if not space:
        return None
    if "." in space:
        return space
    return f"{space}.signalwire.com"


def find_resource_address(addresses, agent_name):
    """
    Find the resource address matching /public/{agent_name} from a list of addresses.

    When phone numbers are attached to a handler, multiple addresses exist.
    We want the resource address (e.g., /public/afterhours) not the phone number address.
    """
    expected_address = f"/public/{agent_name}"

    # First, try to find exact match for /public/{agent_name}
    for addr in addresses:
        audio_channel = addr.get("channels", {}).get("audio", "")
        if audio_channel == expected_address:
            return addr

    # Fallback: find any address that looks like a SIP address (not a phone number)
    for addr in addresses:
        audio_channel = addr.get("channels", {}).get("audio", "")
        # SIP addresses start with /public/ and don't contain phone number patterns
        if audio_channel.startswith("/public/") and not any(c.isdigit() for c in audio_channel.split("/")[-1][:3]):
            return addr

    # Last resort: return first address
    return addresses[0] if addresses else None


def build_rest_client():
    """Construct a RestClient from env, or None if credentials are incomplete.

    RestClient() with no args reads SIGNALWIRE_API_TOKEN / SIGNALWIRE_SPACE, which
    do NOT match this demo's SIGNALWIRE_TOKEN / SIGNALWIRE_SPACE_NAME convention --
    so always pass project/token/host explicitly.
    """
    sw_host = get_signalwire_host()
    project = os.getenv("SIGNALWIRE_PROJECT_ID", "")
    token = os.getenv("SIGNALWIRE_TOKEN", "")
    if not all([sw_host, project, token]):
        return None
    return RestClient(project=project, token=token, host=sw_host)


def find_existing_handler(client, agent_name):
    """Find an existing SWML handler by name via the SDK RestClient."""
    try:
        # swml_webhooks == External SWML Handler; response shape matches the
        # legacy REST endpoint 1:1.
        handlers = client.fabric.swml_webhooks.list().get("data", [])

        for handler in handlers:
            swml_webhook = handler.get("swml_webhook", {})
            handler_name = swml_webhook.get("name") or handler.get("display_name")

            if handler_name == agent_name:
                handler_id = handler.get("id")
                handler_url = swml_webhook.get("primary_request_url", "")

                addresses = client.fabric.swml_webhooks.list_addresses(handler_id).get("data", [])
                resource_addr = find_resource_address(addresses, agent_name)
                if resource_addr:
                    return {
                        "id": handler_id,
                        "name": handler_name,
                        "url": handler_url,
                        "address_id": resource_addr["id"],
                        "address": resource_addr["channels"]["audio"]
                    }
    except Exception as e:
        logger.error(f"Error finding existing handler: {e}")
    return None


def setup_swml_handler():
    """Set up SWML handler on startup (idempotent; records why on failure)."""
    global swml_setup_error

    agent_name = os.getenv("AGENT_NAME", "afterhours")
    proxy_url = os.getenv("SWML_PROXY_URL_BASE", os.getenv("APP_URL", ""))
    auth_user = os.getenv("SWML_BASIC_AUTH_USER", "signalwire")
    auth_pass = os.getenv("SWML_BASIC_AUTH_PASSWORD", "")

    client = build_rest_client()
    if client is None:
        swml_setup_error = "SignalWire credentials not configured"
        logger.warning(f"{swml_setup_error} - skipping SWML handler setup")
        return

    if not proxy_url:
        swml_setup_error = "SWML_PROXY_URL_BASE/APP_URL not set"
        logger.warning(f"{swml_setup_error} - skipping SWML handler setup")
        return

    if auth_user and auth_pass and "://" in proxy_url:
        scheme, rest = proxy_url.split("://", 1)
        swml_url = f"{scheme}://{auth_user}:{auth_pass}@{rest}/{agent_name}"
    else:
        swml_url = f"{proxy_url}/{agent_name}"

    existing = find_existing_handler(client, agent_name)

    if existing:
        swml_handler_info["id"] = existing["id"]
        swml_handler_info["address_id"] = existing["address_id"]
        swml_handler_info["address"] = existing["address"]

        try:
            client.fabric.swml_webhooks.update(
                existing["id"],
                primary_request_url=swml_url,
                primary_request_method="POST"
            )
            logger.info(f"Updated SWML handler: {existing['name']}")
        except Exception as e:
            logger.error(f"Failed to update handler URL: {e}")

        logger.info(f"Call address: {existing['address']}")
        swml_setup_error = None
        return

    try:
        with warnings.catch_warnings():
            # create() emits a DeprecationWarning steering phone-number setups
            # toward phone_numbers.set_swml_webhook; a standalone dialable handler
            # (guest tokens dial its /public/{name} address) is intentional here.
            warnings.simplefilter("ignore", DeprecationWarning)
            handler = client.fabric.swml_webhooks.create(
                name=agent_name,
                used_for="calling",
                primary_request_url=swml_url,
                primary_request_method="POST"
            )
        handler_id = handler.get("id")
        swml_handler_info["id"] = handler_id

        addresses = client.fabric.swml_webhooks.list_addresses(handler_id).get("data", [])
        resource_addr = find_resource_address(addresses, agent_name)
        if resource_addr:
            swml_handler_info["address_id"] = resource_addr["id"]
            swml_handler_info["address"] = resource_addr["channels"]["audio"]
            logger.info(f"Created SWML handler '{agent_name}' with address: {swml_handler_info.get('address')}")
            swml_setup_error = None
        else:
            swml_setup_error = "No address found for created handler"
            logger.error(swml_setup_error)
    except Exception as e:
        swml_setup_error = f"Failed to create SWML handler: {e}"
        logger.error(swml_setup_error)
        time.sleep(0.5)
        existing = find_existing_handler(client, agent_name)
        if existing:
            swml_handler_info["id"] = existing["id"]
            swml_handler_info["address_id"] = existing["address_id"]
            swml_handler_info["address"] = existing["address"]
            logger.info(f"Found existing SWML handler after retry: {existing['name']}")
            swml_setup_error = None


# ===============================================================================
# Agent Definition
# ===============================================================================

class AfterHoursAgent(AgentBase):
    """
    Wire Heating and Air - After-Hours Emergency Service Agent.

    This agent uses a multi-context workflow to guide callers through
    submitting service requests for HVAC emergencies.
    """

    def __init__(self):
        """Initialize the agent with name and route."""
        super().__init__(
            name="Wire Heating and Air",
            route="/afterhours"
        )

        # Set AI model
        self.set_params({
            "ai_model_62c3bdb19a89": "gpt-oss-120b",
            # DIAGNOSTIC: ships the running conversation (call_log) with every SWAIG
            # request so we can see what ASR actually heard. Remove once resolved.
            "swaig_post_conversation": True,
        })

        # Without this the AI session ends and the caller is left on an open line
        # in silence. On the SDK production checklist and easy to omit, because
        # testing always ends by hanging up from the other side.
        self.add_post_ai_verb("hangup", {})

        self._setup_prompts()
        self._setup_contexts()
        self._setup_functions()

    def _setup_prompts(self):
        """Configure the agent's personality."""
        self.prompt_add_section(
            "Role",
            "You are the after-hours answering service for Wire Heating and Air, "
            "an HVAC company. You help customers report heating and air conditioning "
            "emergencies and collect their information for a callback from dispatch. "
            "Be calm, professional, and reassuring - customers calling after hours "
            "are often stressed about their situation."
        )

        self.prompt_add_section(
            "Global Data",
            "Your working memory for this call is global_data.pending_request. Every "
            "set_ function writes to it, and it travels with the conversation, so it is "
            "always current. Read it before you ask anything: if a field is already "
            "filled, you have the answer and must not ask for it again. If the caller "
            "gives you something you have not recorded yet - in their opening sentence "
            "or anywhere since - call the matching set_ function straight away rather "
            "than waiting for the step that normally asks for it. When they volunteer "
            "several details at once, record all of them, then speak.\n\n"
            "Recording is silent - it is not a reply. After every set_ function, say "
            "something to the caller: acknowledge what you captured and ask for the next "
            "thing still missing from pending_request. Never run two set_ functions back "
            "to back without speaking in between, and never leave the caller in silence "
            "waiting for you.",
            bullets=[
                "pending_request.issue_type - set_issue_type - usually clear from the first sentence",
                "pending_request.is_emergency - set_urgency - usually clear from the problem itself",
                "pending_request.customer_name - set_customer_name",
                "pending_request.service_address - set_service_address",
                "pending_request.callback_primary - set_callback_numbers",
                "pending_request.issue_description - set_issue_description",
            ],
        )

        self.prompt_add_section(
            "Service Request Flow",
            "IMPORTANT: Ask only ONE question at a time and wait for the answer before asking the next. "
            "Never batch multiple questions together. Follow the steps in order - each step will guide you "
            "to the next question. Be patient and let the customer answer each question fully."
        )

        self.prompt_add_section(
            "Emergency Guidelines",
            bullets=[
                "Emergency examples: No heat when below freezing, no AC when dangerously hot, gas smell, carbon monoxide alarm",
                "Non-emergency examples: Unit making noise, not cooling/heating as well as usual, thermostat issues",
                "For emergencies, reassure the customer that dispatch will call back as soon as possible",
                "For gas smells or CO alarms, advise customer to leave the building and call nine one one if needed. Always say it as nine one one, never as a single number"
            ]
        )

        self.prompt_add_section(
            "Rental Properties",
            "If the customer rents, remind them they may need landlord approval for repairs. "
            "Still collect all information - the technician can coordinate with the landlord if needed."
        )

    def _setup_contexts(self):
        """Define multi-context workflow for service request process."""
        contexts = self.define_contexts()

        # -----------------------------------------------------------------------
        # Triage Context - defined FIRST so it is the entry point.
        #
        # This used to sit behind a greeting step. That step could not record
        # anything (only start_service_request was callable), but nothing stopped
        # the model talking: it gathered the whole intake conversationally, banked
        # none of it, then started the real flow and asked for it all again.
        # Greeting here means there is no window in which that can happen.
        # -----------------------------------------------------------------------
        triage = contexts.add_context("triage")
        triage.add_step("assess_urgency") \
            .set_text(
                "Establish whether this is an emergency. The greeting has already invited "
                "the caller to explain, so if what they said makes it clear - no heat in "
                "freezing conditions, no cooling in dangerous heat, a gas smell, anything "
                "unsafe - record it and move on without asking. Only if it is still "
                "unclear, ask: is this an emergency, or a routine repair?"
            ) \
            .set_step_criteria("Customer has indicated whether this is an emergency") \
            .set_functions(["set_urgency", "cancel_flow"]) \
            .set_valid_contexts(["emergency_intake", "service_request", "greeting"])


        # -----------------------------------------------------------------------
        # Greeting Context - Entry point
        # -----------------------------------------------------------------------
        greeting = contexts.add_context("greeting")
        # A step with no set_functions exposes EVERY tool. With 11 of them the
        # model stops reliably picking the right one (the SDK documents the
        # degradation past ~7-8), so it never called start_service_request, never
        # left this step, and collected fields in whatever order it fancied.
        # Whitelisting one tool here is what forces the flow into triage.
        greeting.add_step("welcome") \
            .set_text("Is there anything else I can help you with tonight?") \
            .set_step_criteria("Customer has said whether they need anything further") \
            .set_functions(["start_service_request"]) \
            .set_valid_contexts(["triage"])

        # -----------------------------------------------------------------------
        # Emergency Intake - the short path. Unit details and ownership are
        # deferred to the callback so a technician is paged sooner.
        # -----------------------------------------------------------------------
        emergency = contexts.add_context("emergency_intake")

        emergency.add_step("get_issue_type") \
            .set_text("Record whether it is the air conditioning or the heating system. If the caller has already given this, record it and move on without asking. Otherwise, ask which it is.") \
            .set_step_criteria("Customer has indicated whether it is a heating or an air conditioning issue") \
            .set_functions(["set_issue_type", "cancel_flow"]) \
            .set_valid_steps(["get_customer_name"])

        emergency.add_step("get_customer_name") \
            .set_text("Record the caller's name. If the caller has already given this, record it and move on without asking. Otherwise, ask for it.") \
            .set_step_criteria("Customer has provided their name") \
            .set_functions(["set_customer_name", "cancel_flow"]) \
            .set_valid_steps(["get_service_address"])

        emergency.add_step("get_service_address") \
            .set_text("Record the service address. If the caller has already given this, record it and move on without asking. Otherwise, ask for it, including any apartment or unit number.") \
            .set_step_criteria("Customer has provided the service address") \
            .set_functions(["set_service_address", "cancel_flow"]) \
            .set_valid_steps(["get_callback_numbers"])

        emergency.add_step("get_callback_numbers") \
            .set_text("Record a callback number. If the caller has already given this, record it and move on without asking. Otherwise, ask for the best number for the technician to reach them.") \
            .set_step_criteria("Customer has provided a callback number") \
            .set_functions(["set_callback_numbers", "cancel_flow"]) \
            .set_valid_steps(["get_issue_description"])

        emergency.add_step("get_issue_description") \
            .set_text("Record a brief description of the problem. If the caller has already given this, record it and move on without asking. Otherwise, ask what is happening.") \
            .set_step_criteria("Customer has described the issue") \
            .set_functions(["set_issue_description", "cancel_flow"]) \
            .set_valid_contexts(["confirmation", "greeting"])

        # -----------------------------------------------------------------------
        # Service Request Context - full intake for routine calls
        # -----------------------------------------------------------------------
        service_req = contexts.add_context("service_request")

        service_req.add_step("get_issue_type") \
            .set_text("Record whether it is the air conditioning or the heating system. If the caller has already given this, record it and move on without asking. Otherwise, ask which it is.") \
            .set_step_criteria("Customer has indicated whether it is a heating or an air conditioning issue") \
            .set_functions(["set_issue_type", "cancel_flow"]) \
            .set_valid_steps(["get_customer_name"])

        service_req.add_step("get_customer_name") \
            .set_text("Record the caller's name. If the caller has already given this, record it and move on without asking. Otherwise, ask for it.") \
            .set_step_criteria("Customer has provided their name") \
            .set_functions(["set_customer_name", "cancel_flow"]) \
            .set_valid_steps(["get_service_address"])

        service_req.add_step("get_service_address") \
            .set_text("Record the service address. If the caller has already given this, record it and move on without asking. Otherwise, ask for the full street address and any apartment or unit number.") \
            .set_step_criteria("Customer has provided the service address") \
            .set_functions(["set_service_address", "cancel_flow"]) \
            .set_valid_steps(["get_unit_info"])

        service_req.add_step("get_unit_info") \
            .set_text("Record any unit details: brand, rough age, where it sits. If the caller has already given this, record it and move on without asking. Otherwise, ask, and accept whatever they know.") \
            .set_step_criteria("Customer has provided unit information") \
            .set_functions(["set_unit_info", "cancel_flow"]) \
            .set_valid_steps(["get_ownership"])

        service_req.add_step("get_ownership") \
            .set_text("Record whether they own or rent. If the caller has already given this, record it and move on without asking. Otherwise, ask.") \
            .set_step_criteria("Customer has indicated ownership status") \
            .set_functions(["set_ownership", "cancel_flow"]) \
            .set_valid_steps(["get_callback_numbers"])

        service_req.add_step("get_callback_numbers") \
            .set_text("Record a callback number, and an alternate if offered. If the caller has already given this, record it and move on without asking. Otherwise, ask for the best number to reach them.") \
            .set_step_criteria("Customer has provided callback number(s)") \
            .set_functions(["set_callback_numbers", "cancel_flow"]) \
            .set_valid_steps(["get_issue_description"])

        service_req.add_step("get_issue_description") \
            .set_text("Record a description of the problem. If the caller has already given this, record it and move on without asking. Otherwise, ask them to describe it.") \
            .set_step_criteria("Customer has described the issue") \
            .set_functions(["set_issue_description", "cancel_flow"]) \
            .set_valid_contexts(["confirmation", "greeting"])

        # -----------------------------------------------------------------------
        # Confirmation Context - Review and confirm
        # -----------------------------------------------------------------------
        confirm = contexts.add_context("confirmation")
        confirm.add_step("confirm") \
            .set_text("Please review your service request details.") \
            .set_functions(["confirm_request", "cancel_flow"]) \
            .set_valid_contexts(["greeting"])

    def _setup_functions(self):
        """Define SWAIG functions for service request workflow."""

        # -----------------------------------------------------------------------
        # Start Service Request
        # -----------------------------------------------------------------------
        @self.tool(
            name="start_service_request",
            fillers={"en-US": ["Let me pull up a new service request."]},
            description="Begin a new service request once the caller needs HVAC service."
        )
        def start_service_request(args: dict, raw_data: dict = None) -> SwaigFunctionResult:
            return (
                SwaigFunctionResult(
                    "I'll get a service request started for you. "
                    "Before I take your details - is this an emergency? "
                    "That means no heat in freezing conditions, no cooling in dangerous heat, "
                    "or you smell gas."
                )
                .swml_change_context("triage")
                .update_global_data({"pending_request": {}})
            )

        # -----------------------------------------------------------------------
        # Set Issue Type
        # -----------------------------------------------------------------------
        # -----------------------------------------------------------------------
        # Set Urgency - asked first, and it decides which intake path we take
        # -----------------------------------------------------------------------
        @self.tool(
            name="set_urgency",
            fillers={"en-US": ["Let me note the urgency."]},
            description=(
                "Record whether this is an emergency. Call as soon as the caller has "
                "indicated urgency. An emergency is no heat in freezing conditions, no "
                "cooling in dangerous heat, a gas smell, or anything unsafe."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "is_emergency": {
                        "type": "boolean",
                        "description": "True for an emergency, false for a routine service request."
                    },
                    "gas_smell": {
                        "type": "boolean",
                        "description": "True only if the caller reports smelling gas."
                    }
                },
                "required": ["is_emergency"]
            }
        )
        def set_urgency(args: dict, raw_data: dict = None) -> SwaigFunctionResult:
            is_emergency = bool(args.get("is_emergency", False))
            gas_smell = bool(args.get("gas_smell", False))
            raw_data = raw_data or {}
            global_data = raw_data.get("global_data", {})
            pending = global_data.get("pending_request", {})

            pending["is_emergency"] = is_emergency
            pending["gas_smell"] = gas_smell
            global_data["pending_request"] = pending

            if gas_smell:
                # Safety first: this is the one case where we stop collecting.
                response = (
                    "If you smell gas, please leave the building right now and call your "
                    "gas utility or nine one one from outside. I will still take your details "
                    "so a technician can follow up. "
                )
            elif is_emergency:
                response = (
                    "Understood, I'm treating this as an emergency and I'll page the on-call "
                    "technician as soon as I have your details. I'll keep this brief. "
                )
            else:
                response = "Thanks, I'll take the full details for a routine service visit. "

            target = "emergency_intake" if is_emergency else "service_request"
            response += "First, is this for your air conditioning or your heating system?"

            return (
                SwaigFunctionResult(response)
                .swml_change_context(target)
                .update_global_data(global_data)
            )

        @self.tool(
            name="set_issue_type",
            fillers={"en-US": ["Noting the system type."]},
            description="Record whether the issue is with the air conditioning or the heating system.",
            parameters={
                "type": "object",
                "properties": {
                    "issue_type": {
                        "type": "string",
                        "description": "Type of issue: 'ac_repair' or 'heating_repair'",
                        "enum": ["ac_repair", "heating_repair"]
                    },
                },
                "required": ["issue_type"]
            }
        )
        def set_issue_type(args: dict, raw_data: dict = None) -> SwaigFunctionResult:
            issue_type = args.get("issue_type", "ac_repair")
            raw_data = raw_data or {}
            global_data = raw_data.get("global_data", {})
            pending = global_data.get("pending_request", {})

            pending["issue_type"] = issue_type
            # Urgency is set earlier by set_urgency; read it, never overwrite it.
            is_emergency = bool(pending.get("is_emergency", False))
            global_data["pending_request"] = pending

            # "a air conditioning" reads badly in TTS; pick the article to match.
            issue_name = "an air conditioning" if issue_type == "ac_repair" else "a heating"
            urgency = "emergency" if is_emergency else "service request"

            response = f"I've noted this as {issue_name} {urgency}. "
            if is_emergency:
                response += "We'll prioritize getting a technician to call you back. "
            response += "May I have your name please?"

            return (
                SwaigFunctionResult(response)
                .update_global_data(global_data)
            )

        # -----------------------------------------------------------------------
        # Set Customer Name
        # -----------------------------------------------------------------------
        @self.tool(
            name="set_customer_name",
            fillers={"en-US": ["Let me get your name on the ticket."]},
            description="Record the customer's name.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The customer's name"
                    }
                },
                "required": ["name"]
            }
        )
        def set_customer_name(args: dict, raw_data: dict = None) -> SwaigFunctionResult:
            name = args.get("name", "")
            raw_data = raw_data or {}
            global_data = raw_data.get("global_data", {})
            pending = global_data.get("pending_request", {})

            pending["customer_name"] = name
            global_data["pending_request"] = pending

            return (
                SwaigFunctionResult(
                    f"Thank you, {name}. What is the address where service is needed? "
                    "Please include apartment or unit number if applicable."
                )
                .update_global_data(global_data)
            )

        # -----------------------------------------------------------------------
        # Set Service Address
        # -----------------------------------------------------------------------
        @self.tool(
            name="set_service_address",
            fillers={"en-US": ["Writing down the service address."]},
            description="Record the full service address where the work is needed.",
            parameters={
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Full service address including street, city, state, zip, and apt/unit number"
                    }
                },
                "required": ["address"]
            }
        )
        def set_service_address(args: dict, raw_data: dict = None) -> SwaigFunctionResult:
            address = args.get("address", "")
            raw_data = raw_data or {}
            global_data = raw_data.get("global_data", {})
            pending = global_data.get("pending_request", {})

            pending["service_address"] = address
            global_data["pending_request"] = pending

            # The emergency path skips unit details and ownership, so the next
            # question differs. Keep the spoken prompt aligned with the step chain.
            if pending.get("is_emergency"):
                nxt = "What's the best phone number for the technician to reach you?"
            else:
                nxt = ("Can you tell me about your HVAC unit? Any details help - the brand, "
                       "approximate age, or where it's located like rooftop, basement, or closet.")

            return (
                SwaigFunctionResult(f"Got it, {address}. {nxt}")
                .update_global_data(global_data)
            )

        # -----------------------------------------------------------------------
        # Set Unit Info
        # -----------------------------------------------------------------------
        @self.tool(
            name="set_unit_info",
            fillers={"en-US": ["Noting the unit details."]},
            description="Record details about the HVAC unit: brand, age, location.",
            parameters={
                "type": "object",
                "properties": {
                    "unit_info": {
                        "type": "string",
                        "description": "Information about the HVAC unit (brand, age, location, etc.)"
                    }
                },
                "required": ["unit_info"]
            }
        )
        def set_unit_info(args: dict, raw_data: dict = None) -> SwaigFunctionResult:
            unit_info = args.get("unit_info", "")
            raw_data = raw_data or {}
            global_data = raw_data.get("global_data", {})
            pending = global_data.get("pending_request", {})

            pending["unit_info"] = unit_info
            global_data["pending_request"] = pending

            return (
                SwaigFunctionResult(
                    "Thanks for that information. Do you own or rent this property?"
                )
                .update_global_data(global_data)
            )

        # -----------------------------------------------------------------------
        # Set Ownership
        # -----------------------------------------------------------------------
        @self.tool(
            name="set_ownership",
            fillers={"en-US": ["Noting that down."]},
            description="Record whether the customer owns or rents the property.",
            parameters={
                "type": "object",
                "properties": {
                    "ownership": {
                        "type": "string",
                        "description": "Whether customer owns or rents: 'own' or 'rent'",
                        "enum": ["own", "rent"]
                    }
                },
                "required": ["ownership"]
            }
        )
        def set_ownership(args: dict, raw_data: dict = None) -> SwaigFunctionResult:
            ownership = args.get("ownership", "own")
            raw_data = raw_data or {}
            global_data = raw_data.get("global_data", {})
            pending = global_data.get("pending_request", {})

            pending["ownership"] = ownership
            global_data["pending_request"] = pending

            response = ""
            if ownership == "rent":
                response = "Noted that you rent. Just so you know, you may need landlord approval for repairs, but our technician can help coordinate that. "

            response += "What's the best phone number for our dispatch to call you back?"

            return (
                SwaigFunctionResult(response)
                .update_global_data(global_data)
            )

        # -----------------------------------------------------------------------
        # Set Callback Numbers
        # -----------------------------------------------------------------------
        @self.tool(
            name="set_callback_numbers",
            fillers={"en-US": ["Saving your callback number."]},
            description="Record the callback phone number, and an alternate if given.",
            parameters={
                "type": "object",
                "properties": {
                    "primary": {
                        "type": "string",
                        "description": "Primary callback phone number"
                    },
                    "alternate": {
                        "type": "string",
                        "description": "Alternate callback phone number (optional)"
                    }
                },
                "required": ["primary"]
            }
        )
        def set_callback_numbers(args: dict, raw_data: dict = None) -> SwaigFunctionResult:
            primary = args.get("primary", "")
            alternate = args.get("alternate", "")
            raw_data = raw_data or {}
            global_data = raw_data.get("global_data", {})
            pending = global_data.get("pending_request", {})

            pending["callback_primary"] = primary
            if alternate:
                pending["callback_alternate"] = alternate
            global_data["pending_request"] = pending

            response = f"I have {say_phone(primary)} as your callback number"
            if alternate:
                response += f" with {say_phone(alternate)} as a backup"
            response += ". Now, please describe the problem you're experiencing with your system."

            return (
                SwaigFunctionResult(response)
                .update_global_data(global_data)
            )

        # -----------------------------------------------------------------------
        # Set Issue Description
        # -----------------------------------------------------------------------
        @self.tool(
            name="set_issue_description",
            fillers={"en-US": ["Writing up the problem description."]},
            description="Record the caller's description of the problem.",
            parameters={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Detailed description of the HVAC problem"
                    }
                },
                "required": ["description"]
            }
        )
        def set_issue_description(args: dict, raw_data: dict = None) -> SwaigFunctionResult:
            description = args.get("description", "")
            raw_data = raw_data or {}
            global_data = raw_data.get("global_data", {})
            pending = global_data.get("pending_request", {})

            pending["issue_description"] = description
            global_data["pending_request"] = pending

            # Build confirmation summary
            name = pending.get("customer_name", "Customer")
            address = pending.get("service_address", "")
            issue_type = "Air conditioning" if pending.get("issue_type") == "ac_repair" else "Heating"
            urgency = "Emergency" if pending.get("is_emergency") else "Non-emergency"
            primary = pending.get("callback_primary", "")

            summary = (
                f"Let me confirm your service request: "
                f"{name}, at {address}. "
                f"{issue_type} issue - {urgency}. "
                f"We'll call you back at {primary}. "
                f"Issue: {description}. "
                "Is all of this correct?"
            )

            return (
                SwaigFunctionResult(summary)
                .swml_change_context("confirmation")
                .update_global_data(global_data)
            )

        # -----------------------------------------------------------------------
        # Confirm Request
        # -----------------------------------------------------------------------
        @self.tool(
            name="confirm_request",
            fillers={"en-US": ["Submitting your service request now."]},
            description="Finalize and submit the service request."
        )
        def confirm_request(args: dict, raw_data: dict = None) -> SwaigFunctionResult:
            raw_data = raw_data or {}
            global_data = raw_data.get("global_data", {})
            pending = global_data.get("pending_request", {})

            # Validate required fields
            required = ["customer_name", "service_address", "issue_type", "callback_primary", "issue_description"]
            missing = [f for f in required if not pending.get(f)]
            if missing:
                return SwaigFunctionResult(
                    f"I'm missing some information: {', '.join(missing)}. Let me get those details."
                )

            # Create the service request
            ticket_number = generate_ticket_number()
            service_request = {
                "id": ticket_number,
                "customer_name": pending["customer_name"],
                "service_address": pending["service_address"],
                "unit_info": pending.get("unit_info", ""),
                "ownership": pending.get("ownership", "unknown"),
                "callback_primary": pending["callback_primary"],
                "callback_alternate": pending.get("callback_alternate", ""),
                "issue_type": pending["issue_type"],
                "is_emergency": pending.get("is_emergency", False),
                "gas_smell": pending.get("gas_smell", False),
                "issue_description": pending["issue_description"],
                "created_at": datetime.utcnow().isoformat(),
                "status": "pending"
            }

            _save_request(ticket_number, service_request)

            paged = False
            if service_request["is_emergency"]:
                paged = _page_technician(service_request)

            # Clear pending request
            global_data["pending_request"] = {}
            global_data["last_request_id"] = ticket_number

            if service_request["is_emergency"]:
                urgency_msg = "right away - I've paged the on-call technician" if paged \
                    else "right away - the on-call technician is being notified now"
            else:
                urgency_msg = "during our next available slot"

            # Use say_digits for TTS-friendly pronunciation
            spoken_number = say_digits(ticket_number)
            result = SwaigFunctionResult(
                f"Your service request has been submitted. "
                f"Your ticket number is {spoken_number}. "
                f"Our dispatch team will call you back {urgency_msg}. "
                "Is there anything else I can help you with?"
            )
            result.update_global_data(global_data)

            # Send event to frontend
            result.swml_user_event({
                "type": "request_submitted",
                "request": service_request
            })

            return result

        # -----------------------------------------------------------------------
        # Cancel Flow
        # -----------------------------------------------------------------------
        @self.tool(
            name="cancel_flow",
            fillers={"en-US": ["No problem, clearing that."]},
            description="Cancel the current action and return to the main menu."
        )
        def cancel_flow(args: dict, raw_data: dict = None) -> SwaigFunctionResult:
            return (
                SwaigFunctionResult(
                    "No problem. Is there anything else I can help you with?"
                )
                .swml_change_context("greeting")
                .update_global_data({"pending_request": {}})
            )

    def on_function_call(self, name, args, raw_data=None):
        """DIAGNOSTIC: log what ASR heard before running the function."""
        try:
            raw = raw_data or {}
            log = raw.get("call_log") or raw.get("raw_call_log") or []
            cid = raw.get("call_id", "?")
            print(f"[transcript] fn={name} call_id={cid} turns={len(log)}", flush=True)
            for turn in log[-12:]:
                role = turn.get("role", "?")
                content = (turn.get("content") or "").replace("\n", " ")
                if content:
                    print(f"[transcript]   {role}: {content[:300]}", flush=True)
            if not log:
                print(f"[transcript]   (empty) raw keys: {sorted(raw.keys())[:20]}", flush=True)
        except Exception as e:
            print(f"[transcript] logging failed: {e}", flush=True)
        return super().on_function_call(name, args, raw_data)

    def on_swml_request(self, request_data, callback_path, request=None):
        """Configure dynamic settings for each request."""
        self.set_param("end_of_speech_timeout", 700)

        # Spoken verbatim before the model takes over, so the opening line is
        # identical on every call instead of being re-improvised each time.
        self.set_param(
            "static_greeting",
            "Thank you for calling Wire Heating and Air after-hours emergency service. "
            "How can I help you today?"
        )
        # Let callers talk over it - people who are cold or smell gas should not
        # have to wait out a greeting.
        self.set_param("static_greeting_no_barge", False)
        # Without this the agent speaks the greeting and then waits for the
        # caller before doing anything, so the first step question is never
        # asked. The SDK's own survey prefab pairs the two for this reason.
        self.set_param("wait_for_user", False)

        # The browser needs time to attach remoteStream$ and clear the autoplay
        # gate; audio sent before that is lost, not buffered, so the caller hears
        # half the greeting and answers "Hello?". The SDK pitfall entry cites a
        # measured 1715ms to audible with 1200ms still clipping, hence 2000.
        self.set_param("initial_sleep_ms", 2000)

        # NOTE: set_internal_fillers() was removed here while isolating a hang -
        # calls were answering with zero media after it was added. Re-add only
        # once a call is confirmed working without it.

        # The platform default (5s) cut callers off mid-sentence: one answered
        # "I wanna..." and the timeout fired before they finished, so ownership
        # was recorded from a guess.
        self.set_param("attention_timeout", 15000)

        base_url = self.get_full_url(include_auth=False)

        if base_url:
            self.set_param("video_idle_file", f"{base_url}/sigmond_pc_idle.mp4")
            self.set_param("video_talking_file", f"{base_url}/sigmond_pc_talking.mp4")

        # Optional post-prompt URL from environment
        post_prompt_url = os.environ.get("POST_PROMPT_URL")
        if post_prompt_url:
            self.set_post_prompt(
                "Summarize the after-hours service call including: "
                "whether a service request was submitted; "
                "the customer name, address, and callback number; "
                "the type of issue (AC or heating) and whether it was an emergency; "
                "and a brief description of the reported problem."
            )
            self.set_post_prompt_url(post_prompt_url)

        # Dynamic voice selection from file store
        default_voice = "inworld.Elizabeth:inworld-tts-1.5-max"
        selected_voice = get_stored_voice() or default_voice
        print(f"Using voice: {selected_voice}", flush=True)

        # Clear existing languages before adding
        if hasattr(self, '_languages'):
            self._languages = []

        self.add_language(
            name="English",
            code="en-US",
            voice=selected_voice
        )
        self._languages[-1]["params"] = {"streaming": True}

        self.add_hints([
            "Wire Heating and Air",
            "air conditioning", "AC", "heating", "furnace",
            "emergency", "no heat", "no cooling",
            "thermostat", "HVAC"
        ])

        return super().on_swml_request(request_data, callback_path, request)


# ===============================================================================
# Server Creation
# ===============================================================================

def create_server(port=None):
    """Create AgentServer with static file mounting and API endpoints."""
    server = AgentServer(host=HOST, port=port or PORT)

    agent = AfterHoursAgent()
    server.register(agent, "/afterhours")

    web_dir = Path(__file__).parent / "web"
    if web_dir.exists():
        server.serve_static_files(str(web_dir))

    # -------------------------------------------------------------------------
    # Health Check Endpoint
    # -------------------------------------------------------------------------
    @server.app.get("/health")
    def health_check():
        """Health check endpoint for deployment verification."""
        return {"status": "healthy", "agent": "afterhours"}

    @server.app.get("/ready")
    def ready_check():
        """Readiness check - verifies SWML handler is configured."""
        if swml_handler_info.get("address"):
            return {"status": "ready", "address": swml_handler_info["address"]}
        return {"status": "initializing"}

    # -------------------------------------------------------------------------
    # Token Generation Endpoint
    # -------------------------------------------------------------------------
    @server.app.get("/api/version")
    def version_info():
        """Which commit this instance is actually running."""
        return JSONResponse(content=resolve_commit())

    @server.app.get("/get_token")
    def get_token(voice: str = "inworld.Elizabeth:inworld-tts-1.5-max"):
        """Generate a guest token for the web client."""
        # Store the selected voice for use in SWML requests
        set_stored_voice(voice)
        print(f"Stored voice selection: {voice}", flush=True)

        # Handler registration may have been skipped/failed at startup (e.g. proxy
        # URL not yet set). Lazily retry once, serialized so workers don't race.
        if not swml_handler_info.get("address_id"):
            with _swml_setup_lock:
                if not swml_handler_info.get("address_id"):
                    setup_swml_handler()

        if not swml_handler_info.get("address_id"):
            return JSONResponse(
                {"error": f"SWML handler not registered: {swml_setup_error or 'check startup logs'}"},
                status_code=500
            )

        client = build_rest_client()
        if client is None:
            return JSONResponse({"error": "SignalWire credentials not configured"}, status_code=500)

        try:
            expire_at = int(time.time()) + 3600 * 24
            guest = client.fabric.tokens.create_guest_token(
                allowed_addresses=[swml_handler_info["address_id"]],
                expire_at=expire_at
            )
            return {
                "token": guest.get("token", ""),
                "address": swml_handler_info["address"]
            }
        except Exception as e:
            logger.error(f"Token request failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # -------------------------------------------------------------------------
    # Debug Endpoint
    # -------------------------------------------------------------------------
    @server.app.get("/get_resource_info")
    def get_resource_info():
        """Return SWML handler info for debugging."""
        return swml_handler_info

    # -------------------------------------------------------------------------
    # Config Endpoint
    # -------------------------------------------------------------------------
    @server.app.get("/api/config")
    def get_config():
        """Return public configuration for the frontend."""
        phone_number = os.getenv("PHONE_NUMBER", "")
        return {
            "phone_number": phone_number if phone_number else None,
            "company_name": "Wire Heating and Air"
        }

    # -------------------------------------------------------------------------
    # Service Requests API Endpoints
    # -------------------------------------------------------------------------
    @server.app.get("/api/requests")
    def get_requests():
        """Return all service requests sorted by creation time."""
        requests_list = list(_load_requests().values())
        # Sort by created_at descending (newest first)
        requests_list.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        # Separate emergency and non-emergency
        emergency = [r for r in requests_list if r.get("is_emergency")]
        non_emergency = [r for r in requests_list if not r.get("is_emergency")]

        return {
            "requests": requests_list,
            "emergency_count": len(emergency),
            "total_count": len(requests_list)
        }

    @server.app.get("/api/requests/{request_id}")
    def get_request(request_id: str):
        """Return a single service request by ID."""
        _all = _load_requests()
        if request_id in _all:
            return _all[request_id]
        return JSONResponse({"error": "Request not found"}, status_code=404)

    # -------------------------------------------------------------------------
    # Startup: Register SWML handler
    # -------------------------------------------------------------------------
    setup_swml_handler()

    return server


# ===============================================================================
# Module-Level Exports
# ===============================================================================

server = create_server()
app = server.app


# ===============================================================================
# Main Entry Point
# ===============================================================================

if __name__ == "__main__":
    server.run()
