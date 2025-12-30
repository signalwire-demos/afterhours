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
import requests
import random
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# -------------------------------------------------------------------------------
# SignalWire Agents SDK imports
# -------------------------------------------------------------------------------
from signalwire_agents import AgentBase, AgentServer, SwaigFunctionResult

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

# -------------------------------------------------------------------------------
# Service Request Data Structures (In-Memory)
# -------------------------------------------------------------------------------
SERVICE_REQUESTS = {}


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


def find_existing_handler(sw_host, auth, agent_name):
    """Find an existing SWML handler by name."""
    try:
        resp = requests.get(
            f"https://{sw_host}/api/fabric/resources/external_swml_handlers",
            auth=auth,
            headers={"Accept": "application/json"}
        )
        if resp.status_code != 200:
            logger.warning(f"Failed to list handlers: {resp.status_code}")
            return None

        handlers = resp.json().get("data", [])

        for handler in handlers:
            swml_webhook = handler.get("swml_webhook", {})
            handler_name = swml_webhook.get("name") or handler.get("display_name")

            if handler_name == agent_name:
                handler_id = handler.get("id")
                handler_url = swml_webhook.get("primary_request_url", "")

                addr_resp = requests.get(
                    f"https://{sw_host}/api/fabric/resources/external_swml_handlers/{handler_id}/addresses",
                    auth=auth,
                    headers={"Accept": "application/json"}
                )
                if addr_resp.status_code == 200:
                    addresses = addr_resp.json().get("data", [])
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
    """Set up SWML handler on startup."""
    sw_host = get_signalwire_host()
    project = os.getenv("SIGNALWIRE_PROJECT_ID", "")
    token = os.getenv("SIGNALWIRE_TOKEN", "")
    agent_name = os.getenv("AGENT_NAME", "afterhours")

    proxy_url = os.getenv("SWML_PROXY_URL_BASE", os.getenv("APP_URL", ""))
    auth_user = os.getenv("SWML_BASIC_AUTH_USER", "signalwire")
    auth_pass = os.getenv("SWML_BASIC_AUTH_PASSWORD", "")

    if not all([sw_host, project, token]):
        logger.warning("SignalWire credentials not configured - skipping SWML handler setup")
        return

    if not proxy_url:
        logger.warning("SWML_PROXY_URL_BASE/APP_URL not set - skipping SWML handler setup")
        return

    if auth_user and auth_pass and "://" in proxy_url:
        scheme, rest = proxy_url.split("://", 1)
        swml_url = f"{scheme}://{auth_user}:{auth_pass}@{rest}/{agent_name}"
    else:
        swml_url = f"{proxy_url}/{agent_name}"

    auth = (project, token)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    existing = find_existing_handler(sw_host, auth, agent_name)

    if existing:
        swml_handler_info["id"] = existing["id"]
        swml_handler_info["address_id"] = existing["address_id"]
        swml_handler_info["address"] = existing["address"]

        try:
            update_resp = requests.put(
                f"https://{sw_host}/api/fabric/resources/external_swml_handlers/{existing['id']}",
                json={
                    "primary_request_url": swml_url,
                    "primary_request_method": "POST"
                },
                auth=auth,
                headers=headers
            )
            update_resp.raise_for_status()
            logger.info(f"Updated SWML handler: {existing['name']}")
        except Exception as e:
            logger.error(f"Failed to update handler URL: {e}")

        logger.info(f"Call address: {existing['address']}")
    else:
        try:
            handler_resp = requests.post(
                f"https://{sw_host}/api/fabric/resources/external_swml_handlers",
                json={
                    "name": agent_name,
                    "used_for": "calling",
                    "primary_request_url": swml_url,
                    "primary_request_method": "POST"
                },
                auth=auth,
                headers=headers
            )
            handler_resp.raise_for_status()
            handler_id = handler_resp.json().get("id")
            swml_handler_info["id"] = handler_id

            addr_resp = requests.get(
                f"https://{sw_host}/api/fabric/resources/external_swml_handlers/{handler_id}/addresses",
                auth=auth,
                headers={"Accept": "application/json"}
            )
            addr_resp.raise_for_status()
            addresses = addr_resp.json().get("data", [])
            resource_addr = find_resource_address(addresses, agent_name)
            if resource_addr:
                swml_handler_info["address_id"] = resource_addr["id"]
                swml_handler_info["address"] = resource_addr["channels"]["audio"]

            logger.info(f"Created SWML handler '{agent_name}' with address: {swml_handler_info.get('address')}")
        except Exception as e:
            logger.error(f"Failed to create SWML handler: {e}")
            time.sleep(0.5)
            existing = find_existing_handler(sw_host, auth, agent_name)
            if existing:
                swml_handler_info["id"] = existing["id"]
                swml_handler_info["address_id"] = existing["address_id"]
                swml_handler_info["address"] = existing["address"]
                logger.info(f"Found existing SWML handler after retry: {existing['name']}")


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
                "For gas smells or CO alarms, advise customer to leave the building and call 911 if needed"
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
        # Greeting Context - Entry point
        # -----------------------------------------------------------------------
        greeting = contexts.add_context("greeting")
        greeting.add_step("welcome") \
            .set_text(
                "Thank you for calling Wire Heating and Air after-hours emergency service. "
                "Are you experiencing a heating or air conditioning problem?"
            ) \
            .set_step_criteria("Customer indicates they need service") \
            .set_valid_steps(["next"])
        greeting.add_step("ready") \
            .set_text("I can help you with that. Let me get some information.") \
            .set_functions(["start_service_request"])

        # -----------------------------------------------------------------------
        # Service Request Context - Collect details one question at a time
        # -----------------------------------------------------------------------
        service_req = contexts.add_context("service_request")

        service_req.add_step("get_issue_type") \
            .set_text("First, is this for your air conditioning or heating system? And would you consider this an emergency?") \
            .set_step_criteria("Customer has indicated issue type (AC or heating) and emergency status") \
            .set_functions(["set_issue_type", "cancel_flow"]) \
            .set_valid_steps(["get_customer_name"])

        service_req.add_step("get_customer_name") \
            .set_text("May I have your name please?") \
            .set_step_criteria("Customer has provided their name") \
            .set_functions(["set_customer_name", "cancel_flow"]) \
            .set_valid_steps(["get_service_address"])

        service_req.add_step("get_service_address") \
            .set_text("What is the service address? Please include the full street address and any apartment or unit number.") \
            .set_step_criteria("Customer has provided the service address") \
            .set_functions(["set_service_address", "cancel_flow"]) \
            .set_valid_steps(["get_unit_info"])

        service_req.add_step("get_unit_info") \
            .set_text("Can you tell me about your HVAC unit - the brand if you know it, approximately how old it is, and where it's located?") \
            .set_step_criteria("Customer has provided unit information") \
            .set_functions(["set_unit_info", "cancel_flow"]) \
            .set_valid_steps(["get_ownership"])

        service_req.add_step("get_ownership") \
            .set_text("Do you own or rent this property?") \
            .set_step_criteria("Customer has indicated ownership status") \
            .set_functions(["set_ownership", "cancel_flow"]) \
            .set_valid_steps(["get_callback_numbers"])

        service_req.add_step("get_callback_numbers") \
            .set_text("What's the best phone number for our technician to reach you? And is there an alternate number?") \
            .set_step_criteria("Customer has provided callback number(s)") \
            .set_functions(["set_callback_numbers", "cancel_flow"]) \
            .set_valid_steps(["get_issue_description"])

        service_req.add_step("get_issue_description") \
            .set_text("Please describe the problem you're experiencing with your system.") \
            .set_step_criteria("Customer has described the issue") \
            .set_functions(["set_issue_description", "cancel_flow"])

        # -----------------------------------------------------------------------
        # Confirmation Context - Review and confirm
        # -----------------------------------------------------------------------
        confirm = contexts.add_context("confirmation")
        confirm.add_step("confirm") \
            .set_text("Please review your service request details.") \
            .set_functions(["confirm_request", "cancel_flow"])

    def _setup_functions(self):
        """Define SWAIG functions for service request workflow."""

        # -----------------------------------------------------------------------
        # Start Service Request
        # -----------------------------------------------------------------------
        @self.tool(
            name="start_service_request",
            description="Start collecting a new service request. Use when customer needs HVAC service. After this, collect: issue type, name, address, unit info, ownership, callback numbers, then issue description."
        )
        def start_service_request(args: dict, raw_data: dict = None) -> SwaigFunctionResult:
            return (
                SwaigFunctionResult(
                    "I'll get a service request started for you. "
                    "First, is this for your air conditioning or heating system? "
                    "And would you consider this an emergency situation?"
                )
                .swml_change_context("service_request")
                .update_global_data({"pending_request": {}})
            )

        # -----------------------------------------------------------------------
        # Set Issue Type
        # -----------------------------------------------------------------------
        @self.tool(
            name="set_issue_type",
            description="Record the type of issue (AC or heating) and whether it's an emergency. After setting, ask for customer name.",
            parameters={
                "type": "object",
                "properties": {
                    "issue_type": {
                        "type": "string",
                        "description": "Type of issue: 'ac_repair' or 'heating_repair'",
                        "enum": ["ac_repair", "heating_repair"]
                    },
                    "is_emergency": {
                        "type": "boolean",
                        "description": "True if this is an emergency (no heat in freezing temps, no AC in dangerous heat, gas smell, etc.)"
                    }
                },
                "required": ["issue_type", "is_emergency"]
            }
        )
        def set_issue_type(args: dict, raw_data: dict = None) -> SwaigFunctionResult:
            issue_type = args.get("issue_type", "ac_repair")
            is_emergency = args.get("is_emergency", False)
            raw_data = raw_data or {}
            global_data = raw_data.get("global_data", {})
            pending = global_data.get("pending_request", {})

            pending["issue_type"] = issue_type
            pending["is_emergency"] = is_emergency
            global_data["pending_request"] = pending

            issue_name = "air conditioning" if issue_type == "ac_repair" else "heating"
            urgency = "emergency" if is_emergency else "service request"

            response = f"I've noted this as a {issue_name} {urgency}. "
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
            description="Record the customer's name. After setting name, ask for service address.",
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
            description="Record the full service address. After setting address, ask about the HVAC unit.",
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

            return (
                SwaigFunctionResult(
                    f"Got it, {address}. Can you tell me about your HVAC unit? "
                    "Any details help - the brand, approximate age, or where it's located like rooftop, basement, or closet."
                )
                .update_global_data(global_data)
            )

        # -----------------------------------------------------------------------
        # Set Unit Info
        # -----------------------------------------------------------------------
        @self.tool(
            name="set_unit_info",
            description="Record information about the HVAC unit. After setting, ask if they own or rent.",
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
            description="Record whether the customer owns or rents the property. After setting, ask for callback number.",
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
            description="Record callback phone number(s). After setting, ask for issue description.",
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

            response = f"I have {primary} as your callback number"
            if alternate:
                response += f" with {alternate} as a backup"
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
            description="Record the detailed description of the issue. This completes collection and moves to confirmation.",
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
                "issue_description": pending["issue_description"],
                "created_at": datetime.utcnow().isoformat(),
                "status": "pending"
            }

            SERVICE_REQUESTS[ticket_number] = service_request

            # Clear pending request
            global_data["pending_request"] = {}
            global_data["last_request_id"] = ticket_number

            urgency_msg = "as soon as possible" if service_request["is_emergency"] else "shortly"

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

    def on_swml_request(self, request_data, callback_path, request=None):
        """Configure dynamic settings for each request."""
        self.set_param("end_of_speech_timeout", 700)

        base_url = self.get_full_url(include_auth=False)

        if base_url:
            self.set_param("video_idle_file", f"{base_url}/hvac_idle.mp4")
            self.set_param("video_talking_file", f"{base_url}/hvac_talking.mp4")

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

        self.add_language(
            name="English",
            code="en-US",
            voice="elevenlabs.adam"
        )

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
    @server.app.get("/get_token")
    def get_token():
        """Generate a guest token for the web client."""
        sw_host = get_signalwire_host()
        project = os.getenv("SIGNALWIRE_PROJECT_ID", "")
        token = os.getenv("SIGNALWIRE_TOKEN", "")

        if not all([sw_host, project, token]):
            return {"error": "SignalWire credentials not configured"}, 500

        if not swml_handler_info.get("address_id"):
            return {"error": "SWML handler not configured yet"}, 500

        auth = (project, token)

        try:
            expire_at = int(time.time()) + 3600 * 24

            guest_resp = requests.post(
                f"https://{sw_host}/api/fabric/guests/tokens",
                json={
                    "allowed_addresses": [swml_handler_info["address_id"]],
                    "expire_at": expire_at
                },
                auth=auth,
                headers={"Content-Type": "application/json", "Accept": "application/json"}
            )
            guest_resp.raise_for_status()
            guest_token = guest_resp.json().get("token", "")

            return {
                "token": guest_token,
                "address": swml_handler_info["address"]
            }
        except Exception as e:
            logger.error(f"Token request failed: {e}")
            return {"error": str(e)}, 500

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
        requests_list = list(SERVICE_REQUESTS.values())
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
        if request_id in SERVICE_REQUESTS:
            return SERVICE_REQUESTS[request_id]
        return {"error": "Request not found"}, 404

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
