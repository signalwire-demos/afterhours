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
DEBUG_EVENT_LOG = os.environ.get("DEBUG_EVENT_LOG", "/tmp/afterhours_debug_events.jsonl")
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

# The empathy line has to ride on the FIRST GATHER QUESTION.
#
# Measured on call 60b17f9d: set_urgency's response text ("I'm treating this as
# an emergency... let me take a few details") was in the SWAIG payload three
# times and was NEVER SPOKEN. swml_change_context into a gather step discards
# the function's response -- the gather takes the turn and asks question one
# immediately. The step's own set_text is swallowed the same way.
#
# So the only place left that is guaranteed to reach the caller is the text of
# the first question. The caller said "my furnace isn't working" and the very
# next thing they heard was "And your name?".
#
# Deliberately scoped to WORDING ONLY. An earlier instruction here told the
# model to reuse values it already had, and it bled across fields -- it filed
# the caller's name as the service address. This one says acknowledge, and says
# explicitly not to supply any value.
ACK = ("Before you ask this, acknowledge the caller's problem in ONE short "
       "sentence using their own words from "
       "global_data.pending_request.issue_description -- say 'furnace' if they "
       "said furnace, 'heat' if they said heat. Name the stake briefly if it is "
       "obvious (no heat means cold). Then ask for the name. Do NOT ask anything "
       "about the problem, do NOT answer on their behalf, and do NOT supply a "
       "value for this or any other field. If issue_description is empty, open "
       "with a brief \"Sorry you're dealing with that\" instead.")


_DIGIT_WORDS = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2",
                "three": "3", "four": "4", "five": "5", "six": "6",
                "seven": "7", "eight": "8", "nine": "9"}


def normalize_spoken_digits(text):
    """Fold spoken digit runs back into numerals.

    ASR returns what was said, and a caller reading out a postcode says
    "one five two two two". Call 6a2c8743 filed the address as
    "Pittsburgh, Pennsylvania one five 222" -- which is what dispatch would
    have been handed.

    Conservative on purpose: only runs of TWO OR MORE adjacent digit words are
    folded, so a street called "Seven Oaks" keeps its name. A trailing pair of
    numeric groups is then joined when the two together make a five-digit
    postcode, which is the shape this actually fails on.
    """
    if not text:
        return text
    import re as _re

    words = "|".join(sorted(_DIGIT_WORDS, key=len, reverse=True))
    # Two or more digit words in a row, separated only by spaces or commas.
    run = _re.compile(r"\b(?:(?:%s)\b[ ,]*){2,}" % words, _re.I)

    def fold(m):
        found = _re.findall(r"[A-Za-z]+", m.group(0).lower())
        digits = "".join(_DIGIT_WORDS[w] for w in found if w in _DIGIT_WORDS)
        trailing = " " if m.group(0).endswith(" ") else ""
        return digits + trailing

    out = run.sub(fold, str(text))
    out = _re.sub(r"\s{2,}", " ", out).strip()
    # "... 15 222" -> "... 15222" when the pair forms a postcode.
    out = _re.sub(r"\b(\d{1,4})\s+(\d{1,4})\s*$",
                  lambda m: (m.group(1) + m.group(2))
                  if len(m.group(1) + m.group(2)) == 5 else m.group(0),
                  out)
    return out


# issue_type is NOT a gather question.
#
# It was, and the caller opened with "my heat's not working" and was then asked
# "is this your air conditioning or your heating system?" -- answered with "I
# just told you". The first attempt to fix that put a "reuse what you already
# know" instruction on the question. That failed twice over: the model asked
# anyway, AND the instruction bled onto neighbouring fields -- it submitted the
# caller's NAME as the service address, marked confirmed, without ever asking.
# A ticket dispatching a technician to "Jim Smith" is far worse than one extra
# question.
#
# So the question is gone. set_urgency captures issue_type during triage, where
# the caller has already said it (measured: it banked heating_repair correctly
# on the same call that misfiled the address), and confirm_request reads from
# either store. If it is genuinely missing, confirm_request says so rather than
# guessing.


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
        # AFTERHOURS_MODEL="" falls back to the platform default. The pinned
        # gpt-oss-120b produced 108 reasoning_only_retry events in a single call
        # (240 LLM round trips for a 5-question form) and began dropping gather
        # answers; the retry filler is the "bear with me" phrase callers hear.
        _model = os.environ.get("AFTERHOURS_MODEL", "gpt-oss-120b").strip()
        if _model:
            print(f"[model] pinned to {_model}", flush=True)
            self.set_params({"ai_model_62c3bdb19a89": _model})
        else:
            print("[model] using platform default", flush=True)

        self.set_params({
            # DIAGNOSTIC: ships the running conversation (call_log) with every SWAIG
            # request so we can see what ASR actually heard. Remove once resolved.
            "swaig_post_conversation": True,
        })

        # Without this the AI session ends and the caller is left on an open line
        # in silence. On the SDK production checklist and easy to omit, because
        # testing always ends by hanging up from the other side.
        # Real-time debug events POSTed to the agent's own /debug_events route.
        # Level 2 adds every LLM request/response and conversation_add, which is
        # what makes a gather_info call inspectable: gather produces no SWAIG
        # calls, so without this the question-and-answer turns are invisible.
        # The SDK builds this URL itself, so it carries the same credentials as
        # the SWAIG and post-prompt URLs (a hand-built one 401s silently).
        self.enable_debug_events(level=2)
        self._setup_debug_capture()

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
            "Your working memory for this call is global_data.pending_request. "
            "set_urgency writes to it, and it travels with the conversation, so it "
            "is always current. Read it before you ask anything: if a field is "
            "already filled, you have the answer and must not ask for it again.\n\n"
            "Recording is silent - it is not a reply. After set_urgency, say "
            "something to the caller rather than leaving them in silence.\n\n"
            "set_urgency is the ONLY set_ function. The rest of the intake is "
            "collected by the gather, which asks one question at a time on its own; "
            "you do not call a function for those.",
            bullets=[
                "pending_request.is_emergency - set_urgency - usually clear from the problem itself",
                "pending_request.issue_type - set_urgency - pass it when they have said which system",
                "pending_request.issue_description - set_urgency - their own words for the problem",
                "pending_request.gas_smell - set_urgency - only if they mention gas",
            ],
        )

        self.prompt_add_section(
            "Use Their Words",
            "The caller has told you what is wrong. Use it. Every reply should sound "
            "like it came from someone who heard the previous sentence.\n\n"
            "Name the actual problem, in the caller's own terms, not a category. "
            "Someone who said \"my heat's not working\" has a heating problem - say "
            "heat, say heating, do not say \"your HVAC system\" or \"the issue you "
            "described\". Someone who said the furnace is making a banging noise "
            "should hear you mention the banging. Mirroring the specific words is "
            "what tells them they were understood; a generic acknowledgement tells "
            "them they were processed.\n\n"
            "global_data.pending_request.issue_description holds what they said. "
            "Read it before every reply and let it shape the wording.",
            bullets=[
                "no heat: cold is the stake. \"Let's get someone out to you while it's this cold.\"",
                "no cooling: heat is the stake. \"I know how rough that gets in this heat.\"",
                "gas smell: safety first, everything else waits",
                "Acknowledge once, briefly, then keep moving - they called to get help, "
                "not to be sympathised with at length",
                "Never re-ask something they already said. If they correct you, say so "
                "plainly once and carry on - do not apologise repeatedly",
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

    def _setup_debug_capture(self):
        """Log debug events, and loudly flag the ones that explain caller-audible faults.

        "Bear with me, I need to rethink that." is not a thinking filler and has
        nothing to do with the model: the media server says it verbatim when the
        model calls a function that is not in the step's active tool set
        (webhook.c, tool_not_found). So every occurrence is a flow bug - a step
        whose set_functions is missing something the model reasonably wants, or
        a gather deactivating the tool it is being asked for.

        The event carries the offending tool name, which turns that phrase from
        noise into a precise pointer at the step that needs fixing.
        """
        @self.on_debug_event
        def _capture(event_type, data):
            try:
                blob = json.dumps(data, default=str)
            except Exception:
                blob = str(data)

            # The name can sit at any depth -- the platform nests the error
            # under its own key -- so walk for it rather than guessing two
            # levels. Without this the alert fires but cannot say WHICH tool,
            # which is the only actionable part of it.
            def _find_tool(node, depth=0):
                if depth > 6 or not isinstance(node, dict):
                    return None
                for k in ("tool", "function", "function_name", "tool_name"):
                    v = node.get(k)
                    if isinstance(v, str) and v:
                        return v
                for v in node.values():
                    if isinstance(v, dict):
                        found = _find_tool(v, depth + 1)
                        if found:
                            return found
                return None

            tool = _find_tool(data) or "?"

            if "tool_not_found" in blob or "non-existent function" in blob:
                print(f"[TOOL_NOT_FOUND] model asked for '{tool}' - not active in the "
                      f"current step. event={event_type} payload={blob[:600]}", flush=True)
            elif event_type in ("llm_error", "function_error"):
                print(f"[{event_type}] {blob[:400]}", flush=True)
            elif event_type in ("context_change", "gather_reject", "swaig_call"):
                print(f"[{event_type}] {blob[:300]}", flush=True)

            try:
                with open(DEBUG_EVENT_LOG, "a") as fh:
                    fh.write(json.dumps({"type": event_type, "data": data}, default=str) + "\n")
            except Exception:
                pass

    def _setup_contexts(self):
        """Define the call flow.

        Redesigned against the SDK guide (contexts_steps_gather):

          - Intake is gather_info, not a chain of hand-rolled steps. Gather asks
            one question at a time and DEACTIVATES every other tool while it
            runs, so the model cannot reorder, skip, or re-ask. That removes by
            construction the whole bug class we hit doing it by hand.
          - Answers land in global_data under output_key="intake". set_urgency
            writes urgency separately to pending_request, so gather can never
            clobber the routing decision that chose this context.
          - Every step declares set_functions explicitly. A step that omits it
            INHERITS the previous step's active set (across context boundaries),
            which the SDK calls the most common bug in multi-step agents.
          - The review step surfaces collected facts with ${global_data...}
            expansion rather than the model recalling them.
        """
        contexts = self.define_contexts()

        # ------------------------------------------------------------------
        # Triage - entry point. Urgency decides which intake runs.
        # ------------------------------------------------------------------
        triage = contexts.add_context("triage")
        triage.add_step("assess_urgency") \
            .set_text(
                "Establish whether this is an emergency, and let the CALLER decide "
                "that - an emergency pages the on-call technician tonight, so it is "
                "their call, not your read of the problem.\n\n"
                "Acknowledge what they told you in their own words, say what you "
                "think, then ask them to settle it. Something like: that sounds "
                "like one to get someone out for tonight - would you like me to "
                "treat it as an emergency, or can it wait for normal business "
                "hours? Record their answer with confirmed_by_caller set to true.\n\n"
                "The one exception is a gas smell or a carbon monoxide alarm. Do "
                "not ask someone who smells gas whether it is urgent - record it "
                "immediately and give them the safety instruction."
            ) \
            .set_step_criteria(
                "The caller has been ASKED whether this is an emergency and has "
                "answered, or has reported a gas smell"
            ) \
            .set_functions(["set_urgency", "cancel_flow"]) \
            .set_valid_contexts(["emergency_intake", "service_request", "greeting"])

        # ------------------------------------------------------------------
        # Emergency intake - the short form. Unit details and ownership are
        # deferred to the callback so a technician is paged sooner.
        # ------------------------------------------------------------------
        emergency = contexts.add_context("emergency_intake")
        emergency.add_step("collect") \
            .set_text(
                "The caller reported: ${global_data.pending_request.issue_description}. "
                "Acknowledge THAT specific problem in their own words before the first "
                "question - briefly, one sentence - then take the few details the "
                "technician needs to get moving. Do not ask which system it is; you "
                "already know."
            ) \
            .set_functions(["cancel_flow"]) \
            .set_gather_info(
                output_key="intake",
                completion_action="next_step",
                prompt="I'll keep this brief so I can page the on-call technician.",
            ) \
            .add_gather_question(
                key="customer_name", question="And your name?",
                prompt=ACK) \
            .add_gather_question(
                key="service_address", confirm=True,
                question="What is the service address, including any apartment or unit number?") \
            .add_gather_question(
                key="callback_primary", confirm=True,
                question="What is the best phone number for the technician to reach you?")

        emergency.add_step("review") \
            .set_text(
                "Read back the request for ${global_data.intake.customer_name} at "
                "${global_data.intake.service_address} - name the actual problem, "
                "${global_data.pending_request.issue_description}, in their words rather "
                "than as a category - then submit it. Do not ask for anything already "
                "collected. Once it is submitted you MUST tell the caller their ticket "
                "number and make sure they have it before the call ends."
            ) \
            .set_step_criteria("Customer has confirmed the details are correct") \
            .set_functions(["confirm_request", "cancel_flow"]) \
            .set_valid_contexts(["greeting"])

        # ------------------------------------------------------------------
        # Routine intake - the full form.
        # ------------------------------------------------------------------
        service_req = contexts.add_context("service_request")
        service_req.add_step("collect") \
            .set_text(
                "The caller reported: ${global_data.pending_request.issue_description}. "
                "Acknowledge THAT specific problem in their own words before the first "
                "question - briefly, one sentence - then take the full details for a "
                "routine service visit. Do not ask which system it is; you already know."
            ) \
            .set_functions(["cancel_flow"]) \
            .set_gather_info(
                output_key="intake",
                completion_action="next_step",
                prompt="I'll take a few details so dispatch can schedule the visit.",
            ) \
            .add_gather_question(
                key="customer_name", question="May I have your name please?",
                prompt=ACK) \
            .add_gather_question(
                key="service_address", confirm=True,
                question="What is the service address? Please include any apartment or unit number.") \
            .add_gather_question(
                key="unit_info",
                question="Can you tell me about the unit - the brand, roughly how old it is, "
                         "and where it sits? Whatever you know is fine.") \
            .add_gather_question(
                key="ownership", question="Do you own or rent the property?") \
            .add_gather_question(
                key="callback_primary", confirm=True,
                question="What is the best phone number for dispatch to reach you?")

        service_req.add_step("review") \
            .set_text(
                "Read back the request for ${global_data.intake.customer_name} at "
                "${global_data.intake.service_address} - name the actual problem, "
                "${global_data.pending_request.issue_description}, in their words rather "
                "than as a category - then submit it. Do not ask for anything already "
                "collected. Once it is submitted you MUST tell the caller their ticket "
                "number and make sure they have it before the call ends."
            ) \
            .set_step_criteria("Customer has confirmed the details are correct") \
            .set_functions(["confirm_request", "cancel_flow"]) \
            .set_valid_contexts(["greeting"])

        # ------------------------------------------------------------------
        # Greeting - only reached after a cancel, as the way back in.
        # ------------------------------------------------------------------
        greeting = contexts.add_context("greeting")
        greeting.add_step("welcome") \
            .set_text(
                "Ask ONCE whether there is anything else you can help with "
                "tonight. If they say no - or anything that means no - thank "
                "them by name if you have it, tell them to call back if "
                "anything changes, and call end_call. Do not ask again and do "
                "not offer a menu: they already have what they called for."
            ) \
            .set_step_criteria("Customer has said whether they need anything further") \
            .set_functions(["start_service_request", "end_call", "cancel_flow"]) \
            .set_valid_contexts(["triage"])

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
                    },
                    "confirmed_by_caller": {
                        "type": "boolean",
                        "description": (
                            "True ONLY if you asked the caller whether this is an "
                            "emergency and they answered. Your own read of the "
                            "problem is not confirmation. False if you are "
                            "inferring it from what they described."
                        )
                    },
                    # The caller has ALREADY described the problem by the time
                    # urgency is clear -- that is how urgency became clear. These
                    # bank it so the gather does not ask for it again.
                    "issue_type": {
                        "type": "string",
                        "enum": ["ac_repair", "heating_repair"],
                        "description": (
                            "Which system, if the caller has already made it clear. "
                            "ac_repair for cooling, heating_repair for heat. Omit "
                            "ONLY if they genuinely have not said."
                        )
                    },
                    "issue_description": {
                        "type": "string",
                        "description": (
                            "The caller's own description of the problem, if they "
                            "have given one. Their words, not a summary. Omit if "
                            "they have not described it yet."
                        )
                    }
                },
                "required": ["is_emergency", "confirmed_by_caller"]
            }
        )
        def set_urgency(args: dict, raw_data: dict = None) -> SwaigFunctionResult:
            is_emergency = bool(args.get("is_emergency", False))
            gas_smell = bool(args.get("gas_smell", False))
            confirmed = bool(args.get("confirmed_by_caller", False))

            # An emergency pages the on-call technician overnight, so it is the
            # caller's call to make, not a guess from the problem description.
            #
            # Measured on call 6521898b: "my air conditioner is not working"
            # became is_emergency=true with nobody asked. Not working is not the
            # same as dangerous, and the difference is someone's night.
            #
            # Enforced here rather than in the prompt because the prompt already
            # said to ask and the model skipped it. Gas is the one exception --
            # you do not ask someone who smells gas whether it is urgent.
            if is_emergency and not confirmed and not gas_smell:
                return SwaigFunctionResult(
                    "Ask the caller directly before recording this. Say what you "
                    "think - that it sounds like something to send someone out "
                    "for tonight - and ask whether they want it treated as an "
                    "emergency, or whether it can wait for normal business "
                    "hours. Then call set_urgency again with their answer and "
                    "confirmed_by_caller set to true."
                )
            raw_data = raw_data or {}
            global_data = raw_data.get("global_data", {})
            pending = global_data.get("pending_request", {})

            pending["is_emergency"] = is_emergency
            pending["gas_smell"] = gas_smell

            # Anything the caller volunteered on the way to establishing urgency
            # is banked HERE, in global_data, and the gather is told to reuse it.
            # Without this the caller says "my AC is out" and is then asked "is
            # this your air conditioning or your heating?" -- the single most
            # irritating thing an intake bot does.
            for key in ("issue_type", "issue_description"):
                val = (args.get(key) or "").strip()
                if val:
                    pending[key] = val
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
            # gather_info asks the first question on entry, so this only hands over.
            response += "Let me take a few details."

            return (
                SwaigFunctionResult(response)
                .swml_change_context(target)
                .update_global_data(global_data)
            )

        @self.tool(
            name="confirm_request",
            fillers={"en-US": ["Submitting your service request now."]},
            description="Finalize and submit the service request."
        )
        def confirm_request(args: dict, raw_data: dict = None) -> SwaigFunctionResult:
            raw_data = raw_data or {}
            global_data = raw_data.get("global_data", {})
            # Two stores, deliberately separate: gather_info writes the intake
            # answers under its output_key, while set_urgency writes the routing
            # decision to pending_request. Keeping them apart means a gather
            # cannot overwrite the urgency that chose which gather to run.
            intake = global_data.get("intake", {}) or {}
            pending = global_data.get("pending_request", {}) or {}

            # Read from EITHER store, intake first.
            #
            # set_urgency banks what the caller said during triage into
            # pending_request, and the gather is told to skip a question whose
            # answer is already there. So a field can legitimately arrive from
            # either side, and checking only intake would reject a complete
            # request as incomplete -- the gather doing its job would break the
            # submission.
            def field(name, default=""):
                v = intake.get(name)
                if v in (None, ""):
                    v = pending.get(name)
                return default if v in (None, "") else v

            def richer(name, default=""):
                """The more informative of the two, not simply the latest.

                Measured on call 60b17f9d: triage captured "furnace isn't
                working" and the gather then asked "briefly, what is happening?"
                and got "Not working." Plain intake-wins precedence shipped the
                worse one, so dispatch received less than the caller actually
                said. Length is a crude proxy for detail, but it is the right
                crude proxy here -- the terse answer is always the lossy one.
                """
                a = str(intake.get(name) or "").strip()
                b = str(pending.get(name) or "").strip()
                best = a if len(a) >= len(b) else b
                return best or default

            required = ["customer_name", "service_address", "issue_type",
                        "callback_primary", "issue_description"]
            missing = [f for f in required if not field(f)]

            # A gather answer can be confirmed and still be wrong. On one call
            # the model submitted the caller's NAME as the service address,
            # marked confirmed, without ever asking -- and a ticket that
            # dispatches a technician to "Jim Smith" is worse than no ticket,
            # because it looks complete. Cheap shape checks, not validation:
            # only reject what cannot possibly be an address.
            addr = normalize_spoken_digits(str(field("service_address")).strip())
            name = str(field("customer_name")).strip()
            if addr and addr.lower() == name.lower():
                missing.append("service_address (the name was recorded instead)")
            elif addr and not any(ch.isdigit() for ch in addr):
                # Every real service address has a number in it somewhere.
                missing.append("service_address (no street number)")

            if missing:
                return SwaigFunctionResult(
                    f"I'm missing some information: {', '.join(missing)}. Let me get those details."
                )

            # Normalise the free-text system answer; gather returns whatever the
            # caller said, not an enum. set_urgency already writes the enum, and
            # both forms land here, so match on the substring either way.
            raw_type = str(field("issue_type")).lower()
            issue_type = "heating_repair" if ("heat" in raw_type or "furnace" in raw_type) \
                else "ac_repair"

            ticket_number = generate_ticket_number()
            service_request = {
                "id": ticket_number,
                "customer_name": field("customer_name"),
                "service_address": addr,
                "unit_info": field("unit_info"),
                "ownership": field("ownership", "unknown"),
                "callback_primary": field("callback_primary"),
                "callback_alternate": field("callback_alternate"),
                "issue_type": issue_type,
                "is_emergency": pending.get("is_emergency", False),
                "gas_smell": pending.get("gas_smell", False),
                "issue_description": richer("issue_description"),
                "created_at": datetime.utcnow().isoformat(),
                "status": "pending"
            }

            _save_request(ticket_number, service_request)

            paged = False
            if service_request["is_emergency"]:
                paged = _page_technician(service_request)

            # Clear pending request
            global_data["pending_request"] = {}
            global_data["intake"] = {}
            global_data["last_request_id"] = ticket_number

            if service_request["is_emergency"]:
                urgency_msg = "right away - I've paged the on-call technician" if paged \
                    else "right away - the on-call technician is being notified now"
            else:
                urgency_msg = "during our next available slot"

            # Use say_digits for TTS-friendly pronunciation
            spoken_number = say_digits(ticket_number)
            result = SwaigFunctionResult(
                f"Your service request is submitted. Read the ticket number to the caller "
                f"clearly and do not skip it: the number is {spoken_number}. "
                f"Say it a second time to be sure: {spoken_number}. "
                f"Then tell them dispatch will call back {urgency_msg}, "
                "and ask if there is anything else."
            )
            result.update_global_data(global_data)
            # Land in the closing context explicitly. Without this the model
            # stayed in the review step with nothing left to do: on call
            # 60b17f9d the caller said "no" after the ticket and got "I'm here
            # to assist you. Please let me know what you need help with", then
            # "I understand you're feeling urgent" -- it improvised because the
            # flow had never told it where it now was.
            result.swml_change_context("greeting")

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
            name="end_call",
            fillers={"en-US": ["Thanks for calling."]},
            description=(
                "End the call. Use ONLY once the caller has what they need and "
                "has said they want nothing further. Say goodbye first - this "
                "hangs up immediately."
            ),
            parameters={"type": "object", "properties": {}}
        )
        def end_call(args: dict, raw_data: dict = None) -> SwaigFunctionResult:
            # The closing step had no way to finish. On call 60b17f9d the caller
            # said no after the ticket and the agent had nothing to call, so it
            # looped generic offers of help at someone trying to hang up.
            return SwaigFunctionResult(
                "Thanks for calling Wire Heating and Air. Someone will be in "
                "touch shortly. Take care."
            ).hangup()

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

        # (enable_thinking left at the platform default: setting it False was
        #  verified to stop get_ideal_strategy entirely and did NOT stop the
        #  "bear with me" phrase, so it was not the source.)

        # DO NOT ENABLE. set_internal_fillers() double-frees a cJSON node in the
        # media server while building the AI app from this document. glibc aborts
        # the whole freeswitch process ("free(): double free detected in tcache 2"),
        # so this takes down every call on that server, not just ours.
        # Reproduced 5/5; filed upstream. Control with the block removed: clean call.
        #
        # Left switchable so the exact offending key can be bisected without a
        # code change: set AFTERHOURS_FILLER_KEYS to a comma-separated subset.
        # DEFAULT IS OFF - unset means no internal_fillers and a working call.
        _filler_keys = [k.strip() for k in
                        os.environ.get("AFTERHOURS_FILLER_KEYS", "").split(",") if k.strip()]
        if _filler_keys:
            _all = {
                "get_ideal_strategy": ["One moment."],
                "next_step":          ["Okay."],
                "change_context":     ["Okay."],
                "wait_seconds":       ["One moment."],
                "check_time":         ["One moment."],
            }
            selected = {k: {"en-US": _all[k]} for k in _filler_keys if k in _all}
            if selected:
                print(f"[bisect] internal_fillers ENABLED for: {sorted(selected)}", flush=True)
                self.set_internal_fillers(selected)

        # Turn-taking. Measured on call 71fcf9b4, which failed outright: 152
        # seconds, 7571 packets of the caller's audio arriving, and exactly ONE
        # speech_detect. The caller could not get a word in.
        #
        # The loop that produced it: 15s of quiet fires attention_timeout, the
        # agent starts talking, the caller starts answering, they are now
        # talking over each other, the caller's speech is discarded, and 15s
        # later it happens again. Five timeouts in one call, the greeting
        # replayed three times -- which to a caller sounds like the line reset
        # and they are starting over.
        #
        # enable_barge is the fix that matters: a caller must be able to
        # interrupt. Without it the agent's own prompting is what deafens it.
        self.set_param("enable_barge", True)

        # 15s was far too aggressive for an after-hours line. Someone standing
        # at their furnace, or finding their address, is not an absent caller.
        # The platform default (5s) was worse still -- it cut one caller off
        # mid-sentence, "I wanna...", and ownership was recorded from a guess.
        self.set_param("attention_timeout", 30000)

        # Without this the platform re-plays static_greeting on every timeout.
        # A check-in should sound like someone who remembers the conversation,
        # not like the call starting again.
        self.set_param(
            "attention_timeout_prompt",
            "The caller has gone quiet. Do NOT repeat the greeting -- they have "
            "heard it, and hearing it again sounds like the line reset. Check in "
            "once, briefly and warmly. If they have already told you the problem, "
            "refer to it in their own words; if you asked a question, ask it again "
            "more simply. Then wait. Give them room - they may be looking at the "
            "unit or finding their address."
        )

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
