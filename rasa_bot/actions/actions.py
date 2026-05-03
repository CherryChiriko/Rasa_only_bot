import logging
import uuid
from typing import Any, Text, Dict, List, Optional
from rasa_sdk import Action, Tracker
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.events import SlotSet

logger = logging.getLogger(__name__)

# --- Actions ---

class ValidateTicketForm(Action):
    def name(self) -> Text:
        return "action_submit_ticket_form"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:

        user_id = tracker.get_slot("user_id")
        details = tracker.get_slot("ticket_details")

        # Instead of a URL, we send a specific payload that Streamlit will catch
        dispatcher.utter_message(
            text=f"PROPOSAL:ID={user_id}|DESC={details}",
            json_message={
                "type": "ticket_proposal",
                "user_id": user_id,
                "description": details
            }
        )
        return []
    
class ActionTransferToHuman(Action):
    def name(self) -> Text:
        return "action_transfer_to_human"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:

        user_message = tracker.latest_message.get('text', "Besoin d'aide (échecs RAG répétés)")
        ticket_url = create_glpi_ticket(user_message)

        if ticket_url:
            dispatcher.utter_message(
                text=f"Je n'ai pas réussi à vous aider malgré mes tentatives. J'ai créé un ticket pour qu'un conseiller reprenne la main : {ticket_url}"
            )
            return [SlotSet("rag_failure_count", 0), SlotSet("glpi_ticket_url", ticket_url)]
        
        dispatcher.utter_message(text="Je vous transfère à un conseiller. Veuillez patienter...")
        return [SlotSet("rag_failure_count", 0)]

class ActionViewTicket(Action):
    def name(self) -> Text:
        return "action_view_ticket"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        
        url = tracker.get_slot("glpi_ticket_url")
        if url:
            dispatcher.utter_message(text=f"Vous pouvez suivre votre demande ici : {url}")
        else:
            dispatcher.utter_message(text="Aucun ticket n'est ouvert pour le moment.")
        return []