"""Optional standalone conversation adapter for the Vera challenge."""

from typing import Optional
from bot import compose, _conversation_reply, CHATS, _get, merchant_name

def respond(state: dict, merchant_message: str, merchant_id: str,
            customer_id: Optional[str] = None,
            trigger_id: Optional[str] = None) -> dict:
    """Apply the same stateful reply policy without HTTP."""
    state.setdefault("id", "local")
    state.setdefault("merchant_id", merchant_id)
    state.setdefault("customer_id", customer_id)
    state.setdefault("trigger_id", trigger_id)
    state.setdefault("turns", [])
    state.setdefault("sent", set())
    state.setdefault("merchant_messages", [])
    state.setdefault("repeat_count", 0)
    state.setdefault("mode", "qualifying")
    state.setdefault("status", "open")

    merchant = _get("merchant", merchant_id) or {}
    category = _get("category", merchant.get("category_slug")) or {}
    customer = _get("customer", customer_id)
    trigger = _get("trigger", trigger_id) or {
        "kind": "conversation_continuation",
        "scope": "merchant",
        "suppression_key": "local",
    }
    state["turns"].append({"from": "merchant", "body": merchant_message})
    return _conversation_reply(state, merchant_message, category, merchant,
                               trigger, customer, "merchant")
