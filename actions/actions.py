import logging
import uuid
import re
import functools
import time
import inspect
import requests
from typing import Any, Text, Dict, List, Optional
from rasa_sdk import Action, Tracker, FormValidationAction
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.events import SlotSet, ActiveLoop
from rasa_sdk.types import DomainDict

logger = logging.getLogger(__name__)

def timed(cls):
    """Class decorator that wraps run() and validate_* methods with timing logs."""
    # 1. Wrap the run method
    original_run = getattr(cls, "run", None)
    if original_run:
        if inspect.iscoroutinefunction(original_run):
            @functools.wraps(original_run)
            async def timed_run_async(self, dispatcher, tracker, domain):
                t0 = time.perf_counter()
                result = await original_run(self, dispatcher, tracker, domain)
                elapsed = (time.perf_counter() - t0) * 1000
                logger.info("[TIMING] %s.run() took %.1f ms (async)", cls.__name__, elapsed)
                return result
            cls.run = timed_run_async
        else:
            @functools.wraps(original_run)
            def timed_run_sync(self, dispatcher, tracker, domain):
                t0 = time.perf_counter()
                result = original_run(self, dispatcher, tracker, domain)
                elapsed = (time.perf_counter() - t0) * 1000
                logger.info("[TIMING] %s.run() took %.1f ms (sync)", cls.__name__, elapsed)
                return result
            cls.run = timed_run_sync
 
    # 2. Wrap validation methods
    for attr_name in list(vars(cls)):
        if attr_name.startswith("validate_"):
            original_fn = getattr(cls, attr_name)
            if callable(original_fn):
                def _make(fn, name):
                    if inspect.iscoroutinefunction(fn):
                        @functools.wraps(fn)
                        async def timed_validate_async(self, slot_value, dispatcher, tracker, domain):
                            t0 = time.perf_counter()
                            result = await fn(self, slot_value, dispatcher, tracker, domain)
                            elapsed = (time.perf_counter() - t0) * 1000
                            logger.info("[TIMING] %s.%s() took %.1f ms (async)", cls.__name__, name, elapsed)
                            return result
                        return timed_validate_async
                    else:
                        @functools.wraps(fn)
                        def timed_validate_sync(self, slot_value, dispatcher, tracker, domain):
                            t0 = time.perf_counter()
                            result = fn(self, slot_value, dispatcher, tracker, domain)
                            elapsed = (time.perf_counter() - t0) * 1000
                            logger.info("[TIMING] %s.%s() took %.1f ms (sync)", cls.__name__, name, elapsed)
                            return result
                        return timed_validate_sync
                setattr(cls, attr_name, _make(original_fn, attr_name))
    return cls
# --- Friendly & Contextual Fallbacks ---
_FALLBACK_MESSAGES: Dict[Optional[str], str] = {
    "user_id": (
        "Je n'ai pas bien saisi votre identifiant. "
        "Pourriez-vous le vérifier ? Il doit être composé de 2 lettres et 4 chiffres (ex: AB4521)."
    ),
    "problem_description": (
        "Je n'ai pas bien compris la description de votre problème. "
        "Pourriez-vous m'en dire un peu plus ou l'exprimer autrement ?"
    ),
    None: (
        "Je suis désolé, je n'ai pas bien compris. "
        "Pourriez-vous reformuler votre demande ?"
    ),
}

@timed
class ActionContextAwareFallback(Action):
    """
    Replaces the generic utter_ask_rephrase.
    """
    def name(self) -> Text:
        return "action_context_aware_fallback"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        requested_slot = tracker.get_slot("requested_slot")
        message = _FALLBACK_MESSAGES.get(
            requested_slot,
            _FALLBACK_MESSAGES[None],
        )
        dispatcher.utter_message(text=message)
        return []

@timed
class ValidateTicketForm(FormValidationAction):
    """Validates slot values as they are filled during the ticket form."""

    def name(self) -> Text:
        return "validate_ticket_form"

    def validate_user_id(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: DomainDict,
    ) -> Dict[Text, Any]:
        """
        Validates the user ID strictly against the AB4521 format.
        """
        if not slot_value or not str(slot_value).strip():
            dispatcher.utter_message(
                text="Pour que je puisse vous identifier, j'ai besoin de votre identifiant (ex: AB4521)."
            )
            return {"user_id": None}

        # Exact match: 2 uppercase letters + 4 digits
        clean_value = str(slot_value).strip().upper()
        is_valid_id = bool(re.match(r"^[A-Z]{2}\d{4}$", clean_value))
        
        if is_valid_id:
            logger.info(f"Validated user_id: {clean_value}")
            return {"user_id": clean_value}
        
        logger.warning(f"Invalid user_id format received: {slot_value}")
        dispatcher.utter_message(
            text="Cet identifiant ne semble pas valide. Il doit contenir 2 lettres suivies de 4 chiffres (ex: AB4521)."
        )
        return {"user_id": None}

    def validate_problem_description(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: DomainDict,
    ) -> Dict[Text, Any]:
        """Reject very short descriptions that are unlikely to be useful."""
        if slot_value and len(str(slot_value).strip()) >= 10:
            logger.info(f"Validated problem_description: {slot_value}")
            return {"problem_description": str(slot_value).strip()}

        dispatcher.utter_message(
            text="Merci pour ces détails, mais pourriez-vous décrire un peu plus précisément le problème (10 caractères minimum) ?"
        )
        return {"problem_description": None}

@timed
class ActionSubmitTicketDraft(Action):
    def name(self) -> Text:
        return "action_submit_ticket_draft"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        user_id = tracker.get_slot("user_id")
        description = tracker.get_slot("problem_description")
        
        if not user_id or not description:
            dispatcher.utter_message(
                text="Oups, il me manque des informations pour préparer votre ticket. Recommençons ensemble."
            )
            return []

        ticket_ref = str(uuid.uuid4())[:8].upper()
        response_string = f"TICKET:ID={user_id}|DESC={description}|REF={ticket_ref}"

        logger.info(f"Submitting ticket draft: {response_string}")
        dispatcher.utter_message(text=response_string)
        return []

@timed
class ActionResetTicketSlots(Action):
    def name(self) -> Text:
        return "action_reset_ticket_slots"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        return [
            SlotSet("user_id", None),
            SlotSet("problem_description", None),
            SlotSet("ticket_confirmed", None),
        ]