import json
import logging
import uuid
import re
from typing import Any, Text, Dict, List, Optional
from rasa_sdk import Action, Tracker, FormValidationAction
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.events import SlotSet, ActiveLoop
from rasa_sdk.types import DomainDict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Device extraction
# ---------------------------------------------------------------------------
_DEVICE_PATTERNS = [
    (re.compile(r"\b(pc|ordinateur|poste)\b",              re.I), "mon PC"),
    (re.compile(r"\b(écran|ecran)\b",                      re.I), "mon écran"),
    (re.compile(r"\b(imprimante)\b",                       re.I), "mon imprimante"),
    (re.compile(r"\b(clavier)\b",                          re.I), "mon clavier"),
    (re.compile(r"\b(souris)\b",                           re.I), "ma souris"),
    (re.compile(r"\b(vpn)\b",                              re.I), "le VPN"),
    (re.compile(r"\b(wifi|wi-fi|réseau|reseau)\b",         re.I), "le réseau"),
    (re.compile(r"\b(outlook|mail|messagerie)\b",          re.I), "Outlook"),
    (re.compile(r"\b(serveur)\b",                          re.I), "le serveur"),
    (re.compile(r"\b(logiciel|application|appli|app)\b",   re.I), "l'application"),
    (re.compile(r"\b(téléphone|telephone|portable)\b",     re.I), "mon téléphone"),
]

def _extract_device(text: str) -> Optional[str]:
    for pattern, label in _DEVICE_PATTERNS:
        if pattern.search(text):
            return label
    return None


# ---------------------------------------------------------------------------
# Description assembly
# ---------------------------------------------------------------------------
_NEGATION_FIXES = [
    # Explicit French reflexive forms first
    (re.compile(r"\bs['’]?allume plus\b", re.I), "ne s'allume plus"),
    (re.compile(r"\bs['’]?allume pas\b",  re.I), "ne s'allume pas"),

    # Generic forms
    (re.compile(r"\bmarche plus\b",               re.I), "ne marche plus"),
    (re.compile(r"\bfonctionne plus\b",           re.I), "ne fonctionne plus"),
    (re.compile(r"\bdémarre plus\b",              re.I), "ne démarre plus"),
    (re.compile(r"\bdemarre plus\b",              re.I), "ne démarre plus"),
    (re.compile(r"\brépond plus\b",               re.I), "ne répond plus"),
    (re.compile(r"\brepond plus\b",               re.I), "ne répond plus"),
    (re.compile(r"\bpeut plus\b",                 re.I), "ne peut plus"),
    (re.compile(r"\bpeux plus\b",                 re.I), "ne peux plus"),
    (re.compile(r"\barrive plus\b",               re.I), "n'arrive plus"),

    (re.compile(r"\bmarche pas\b",                re.I), "ne marche pas"),
    (re.compile(r"\bfonctionne pas\b",            re.I), "ne fonctionne pas"),
]

_FILLER_PREFIX = re.compile(
    r"^(il |elle |ça |ca |c'est |le problème c'est que |le probleme c'est que )",
    re.I,
)

def _clean_detail(text: str) -> str:
    """Strip filler openers and fix dropped negations."""
    text = _FILLER_PREFIX.sub("", text.strip())
    for pattern, replacement in _NEGATION_FIXES:
        text = pattern.sub(replacement, text)
    return text.strip()


def _assemble_description(trigger: str, details: List[str]) -> str:
    """
    Combine the trigger context (device/topic) with one or more detail
    messages into a single readable sentence.

    "J'ai un problème avec mon pc" + ["il s'allume pas", "depuis ce matin"]
      → "Mon PC : ne s'allume pas, depuis ce matin."
    """
    device = _extract_device(trigger)

    # Try to find a device in the detail messages if not in trigger
    if not device:
        for d in details:
            device = _extract_device(d)
            if device:
                break

    cleaned = [_clean_detail(d) for d in details if d.strip()]

    # Merge all detail fragments into one sentence
    merged = ", ".join(cleaned).strip().rstrip(".,;")

    # Capitalise
    if merged:
        merged = merged[0].upper() + merged[1:]

    if device and merged:
        return f"{device[0].upper() + device[1:]} : {merged}."
    elif merged:
        return merged + "."
    elif device:
        return f"Problème avec {device}."
    else:
        return trigger  # Last resort — keep original


# ---------------------------------------------------------------------------
# Generic trigger detection
# ---------------------------------------------------------------------------
_GENERIC_TRIGGERS = [
    "j'ai un problème", "j'ai un probleme",
    "j'ai un autre problème", "j'ai un autre probleme",
    "j'ai encore un problème", "j'ai encore un probleme",
    "j'ai un problème avec mon pc", "j'ai un probleme avec mon pc",
    "j'ai un problème avec mon ordinateur",
    "j'ai un problème avec mon écran", "j'ai un problème avec mon ecran",
    "j'ai un problème de connexion",
    "j'ai besoin d'aide", "j'ai besoin d aide",
    "mon pc ne marche pas", "mon ordinateur ne marche pas",
    "ça ne marche pas", "ca ne marche pas",
    "rien ne marche", "rien ne fonctionne",
    "aide", "help",
    "je veux signaler un problème", "je veux signaler un probleme",
    "il y a un problème", "il y a un probleme",
    "problème technique", "probleme technique",
]

def _is_generic(text: str) -> bool:
    normalized = text.lower().strip().rstrip(".")
    return any(normalized == t or normalized.startswith(t + " ") for t in _GENERIC_TRIGGERS)


# ---------------------------------------------------------------------------
# Fallback messages
# ---------------------------------------------------------------------------
_FALLBACK_MESSAGES: Dict[Optional[str], str] = {
    "user_id": (
        "Je n'ai pas bien saisi votre identifiant. "
        "Il doit être composé de 2 lettres et 4 chiffres (ex: AB4521)."
    ),
    "problem_description": (
        "Je n'ai pas bien compris votre problème. "
        "Pourriez-vous m'en dire un peu plus ?"
    ),
    None: (
        "Je suis désolé, je n'ai pas bien compris. "
        "Pourriez-vous reformuler votre demande ?"
    ),
}


# ---------------------------------------------------------------------------
# Form validation
# ---------------------------------------------------------------------------
class ValidateTicketForm(FormValidationAction):
    def name(self) -> Text:
        return "validate_ticket_form"

    _DESC_MIN_LEN = 10  # minimum chars for a detail message

    def validate_problem_description(
        self, slot_value, dispatcher, tracker, domain
    ) -> Dict[Text, Any]:

        latest_text = tracker.latest_message.get("text", "").strip()

        # User only typed something vague like:
        # "help", "j'ai un problème", etc.
        if _is_generic(latest_text):

            dispatcher.utter_message(
                text=(
                    "Pouvez-vous préciser le problème rencontré ? "
                    "Quel appareil est concerné et que se passe-t-il exactement ?"
                )
            )

            return {
                "problem_description": None
            }

        # Original trigger message
        trigger = tracker.get_slot("trigger_context")

        # First time entering validation
        trigger = tracker.get_slot("trigger_context")

        # Recover the original report message from tracker history
        if not trigger:

            for event in reversed(tracker.events):

                if event.get("event") != "user":
                    continue

                text = (event.get("text") or "").strip()

                # Skip generic short replies
                if not text:
                    continue

                # Keep the FIRST problem report mentioning a device/topic
                if _extract_device(text) or _is_generic(text):
                    trigger = text
                    break

        # Absolute fallback
        if not trigger:
            trigger = latest_text



        # Existing accumulated messages
        raw = tracker.get_slot("detail_messages") or "[]"

        try:
            messages = json.loads(raw)
        except Exception:
            messages = []

        # Avoid duplicates
        if not messages or messages[-1] != latest_text:
            messages.append(latest_text)

        # Need more detail
        combined_len = sum(len(m) for m in messages)

        if combined_len < self._DESC_MIN_LEN:
            dispatcher.utter_message(
                text=(
                    "Pouvez-vous préciser le problème ? "
                    "Quel appareil est concerné et que se passe-t-il exactement ?"
                )
            )

            return {
                "trigger_context": trigger,
                "detail_messages": json.dumps(messages, ensure_ascii=False),
                "problem_description": None,
            }

        # Build intelligent description
        description = _assemble_description(trigger, messages)

        logger.info("Assembled description: %s", description)

        return {
            "trigger_context": trigger,
            "detail_messages": json.dumps(messages, ensure_ascii=False),
            "problem_description": description,
        }

    def validate_user_id(
        self, slot_value, dispatcher, tracker, domain
    ) -> Dict[Text, Any]:
        if not slot_value or not str(slot_value).strip():
            dispatcher.utter_message(
                text="J'ai besoin de votre identifiant pour continuer (ex: AB4521)."
            )
            return {"user_id": None}

        clean = str(slot_value).strip().upper()
        if re.match(r"^[A-Z]{2}\d{4}$", clean):
            logger.info("Validated user_id: %s", clean)
            return {"user_id": clean}

        logger.warning("Invalid user_id: %s", slot_value)
        dispatcher.utter_message(
            text="Cet identifiant ne semble pas valide. Il doit contenir 2 lettres suivies de 4 chiffres (ex: AB4521)."
        )
        return {"user_id": None}


# ---------------------------------------------------------------------------
# Other actions
# ---------------------------------------------------------------------------
class ActionContextAwareFallback(Action):
    def name(self) -> Text:
        return "action_context_aware_fallback"

    def run(self, dispatcher, tracker, domain):
        requested_slot = tracker.get_slot("requested_slot")
        message = _FALLBACK_MESSAGES.get(requested_slot, _FALLBACK_MESSAGES[None])
        dispatcher.utter_message(text=message)
        return []


class ActionSubmitTicketDraft(Action):
    def name(self) -> Text:
        return "action_submit_ticket_draft"

    def run(self, dispatcher, tracker, domain):
        user_id     = tracker.get_slot("user_id")
        description = tracker.get_slot("problem_description")

        if not user_id or not description:
            dispatcher.utter_message(
                text="Il me manque des informations pour préparer votre ticket. Recommençons ensemble."
            )
            return []

        ticket_ref      = str(uuid.uuid4())[:8].upper()
        response_string = f"TICKET:ID={user_id}|DESC={description}|REF={ticket_ref}"

        logger.info("Submitting ticket draft: %s", response_string)
        dispatcher.utter_message(text=response_string)
        return []


class ActionResetTicketSlots(Action):
    def name(self) -> Text:
        return "action_reset_ticket_slots"

    def run(self, dispatcher, tracker, domain):
        logger.info("Resetting ticket slots.")
        dispatcher.utter_message(
            text="Pas de problème ! Dites-moi ce qui ne va pas et nous allons créer un nouveau ticket."
        )
        return [
            SlotSet("problem_description", None),
            SlotSet("trigger_context", None),
            SlotSet("detail_messages", None),
            ActiveLoop("ticket_form"),
        ]


class ActionTicketSubmitted(Action):
    def name(self) -> Text:
        return "action_ticket_submitted"

    def run(self, dispatcher, tracker, domain):
        logger.info("Ticket confirmed and submitted by user.")
        dispatcher.utter_message(
            text="✅ Votre ticket a bien été transmis ! Un technicien va prendre en charge votre demande. Avez-vous un autre problème à signaler ?"
        )
        return [
            SlotSet("problem_description", None),
            SlotSet("trigger_context", None),
            SlotSet("detail_messages", None),
        ]