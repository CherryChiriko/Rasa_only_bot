import logging
import uuid
import re
import functools
import time
import inspect
from typing import Any, Text, Dict, List, Optional
from rasa_sdk import Action, Tracker, FormValidationAction
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.events import SlotSet, ActiveLoop, AllSlotsReset
from rasa_sdk.types import DomainDict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Timing decorator — handles both sync and async methods
# ---------------------------------------------------------------------------
def timed(cls):
    original_run = getattr(cls, "run", None)
    if original_run:
        if inspect.iscoroutinefunction(original_run):
            @functools.wraps(original_run)
            async def timed_run_async(self, dispatcher, tracker, domain):
                t0 = time.perf_counter()
                result = await original_run(self, dispatcher, tracker, domain)
                logger.info("[TIMING] %s.run() took %.1f ms (async)", cls.__name__, (time.perf_counter() - t0) * 1000)
                return result
            cls.run = timed_run_async
        else:
            @functools.wraps(original_run)
            def timed_run_sync(self, dispatcher, tracker, domain):
                t0 = time.perf_counter()
                result = original_run(self, dispatcher, tracker, domain)
                logger.info("[TIMING] %s.run() took %.1f ms (sync)", cls.__name__, (time.perf_counter() - t0) * 1000)
                return result
            cls.run = timed_run_sync

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
                            logger.info("[TIMING] %s.%s() took %.1f ms (async)", cls.__name__, name, (time.perf_counter() - t0) * 1000)
                            return result
                        return timed_validate_async
                    else:
                        @functools.wraps(fn)
                        def timed_validate_sync(self, slot_value, dispatcher, tracker, domain):
                            t0 = time.perf_counter()
                            result = fn(self, slot_value, dispatcher, tracker, domain)
                            logger.info("[TIMING] %s.%s() took %.1f ms (sync)", cls.__name__, name, (time.perf_counter() - t0) * 1000)
                            return result
                        return timed_validate_sync
                setattr(cls, attr_name, _make(original_fn, attr_name))
    return cls


# ---------------------------------------------------------------------------
# Fallback messages keyed by requested slot
# ---------------------------------------------------------------------------
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
    def name(self) -> Text:
        return "action_context_aware_fallback"

    def run(self, dispatcher, tracker, domain):
        requested_slot = tracker.get_slot("requested_slot")
        message = _FALLBACK_MESSAGES.get(requested_slot, _FALLBACK_MESSAGES[None])
        dispatcher.utter_message(text=message)
        return []


@timed
class ValidateTicketForm(FormValidationAction):
    def name(self) -> Text:
        return "validate_ticket_form"

    def validate_user_id(self, slot_value, dispatcher, tracker, domain):
        if not slot_value or not str(slot_value).strip():
            dispatcher.utter_message(
                text="Pour que je puisse vous identifier, j'ai besoin de votre identifiant (ex: AB4521)."
            )
            return {"user_id": None}

        clean_value = str(slot_value).strip().upper()
        if re.match(r"^[A-Z]{2}\d{4}$", clean_value):
            logger.info("Validated user_id: %s", clean_value)
            return {"user_id": clean_value}

        logger.warning("Invalid user_id format received: %s", slot_value)
        dispatcher.utter_message(
            text="Cet identifiant ne semble pas valide. Il doit contenir 2 lettres suivies de 4 chiffres (ex: AB4521)."
        )
        return {"user_id": None}

    def validate_problem_description(self, slot_value, dispatcher, tracker, domain):
        if slot_value and len(str(slot_value).strip()) >= 10:
            logger.info("Validated problem_description: %s", slot_value)
            return {"problem_description": str(slot_value).strip()}

        dispatcher.utter_message(
            text="Merci, mais pourriez-vous décrire un peu plus précisément le problème (10 caractères minimum) ?"
        )
        return {"problem_description": None}


@timed
class ActionSubmitTicketDraft(Action):
    def name(self) -> Text:
        return "action_submit_ticket_draft"

    def run(self, dispatcher, tracker, domain):
        user_id     = tracker.get_slot("user_id")
        description = tracker.get_slot("problem_description")

        if not user_id or not description:
            dispatcher.utter_message(
                text="Oups, il me manque des informations pour préparer votre ticket. Recommençons ensemble."
            )
            return []

        ticket_ref      = str(uuid.uuid4())[:8].upper()
        response_string = f"TICKET:ID={user_id}|DESC={description}|REF={ticket_ref}"

        logger.info("Submitting ticket draft: %s", response_string)
        dispatcher.utter_message(text=response_string)
        return []


@timed
class ActionResetTicketSlots(Action):
    """
    Called by Streamlit when the user clicks '❌ Annuler / Modifier par chat'.
    Clears all ticket slots so the form restarts cleanly on the next report.
    Also re-activates the ticket form so the user can immediately re-describe
    their problem without having to trigger it again.
    """
    def name(self) -> Text:
        return "action_reset_ticket_slots"

    def run(self, dispatcher, tracker, domain):
        logger.info("Resetting ticket slots — user chose to edit via chat.")
        dispatcher.utter_message(
            text="Pas de problème ! Dites-moi ce qui ne va pas et nous allons créer un nouveau ticket."
        )
        return [
            # Keep user_id — the person editing is the same user.
            # Only wipe description so the form re-asks for it.
            SlotSet("problem_description", None),
            # Re-activate the form so the bot immediately asks for the description
            ActiveLoop("ticket_form"),
        ]


@timed
class ActionTicketSubmitted(Action):  # noqa
    """
    Called by Streamlit when the user clicks '🚀 Envoyer au technicien'.
    Clears ticket slots and sends a confirmation message so the conversation
    state is clean for any follow-up.
    """
    def name(self) -> Text:
        return "action_ticket_submitted"

    def run(self, dispatcher, tracker, domain):
        logger.info("Ticket confirmed and submitted by user.")
        dispatcher.utter_message(
            text="✅ Votre ticket a bien été transmis au support technique. Un technicien va prendre en charge votre demande. Avez-vous un autre problème à signaler ?"
        )
        return [
            # Keep user_id — same person may open another ticket right after.
            # Only clear description so the form re-asks for it on the next report.
            SlotSet("problem_description", None),
        ]