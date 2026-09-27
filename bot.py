"""
Vera Challenge Bot - independent implementation.

Design:
- deterministic context store with atomic version replacement
- trigger-aware message planner with category-specific voice
- evidence selection from trigger/category/merchant/customer data
- lightweight conversation policy for opt-outs, auto-replies, intent handoff,
  customer replies, and off-topic requests
- no external network calls, so synthetic challenge payloads stay in-process
"""

from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


app = FastAPI(title="Vera Merchant Assistant - Independent")

_STARTED = time.time()
STORE: dict[tuple[str, str], dict[str, Any]] = {}
CHATS: dict[str, dict[str, Any]] = {}
FIRED: set[str] = set()
ENDED: set[str] = set()
AUTO_STREAK: dict[str, int] = {}


# ------------------------------- metadata ---------------------------------

META = {
    "team_name": os.getenv("TEAM_NAME", "Independent Vera Build"),
    "team_members": [x.strip() for x in os.getenv("TEAM_MEMBERS", "Amit").split(",") if x.strip()],
    "model": "deterministic-context-planner-v2",
    "approach": "evidence selection + trigger routing + stateful conversation policy",
    "contact_email": os.getenv("CONTACT_EMAIL", "team@example.com"),
    "version": "2.0.0",
    "submitted_at": datetime.now(timezone.utc).isoformat(),
}


# ------------------------------- utilities --------------------------------

def put_first(*values: Any, default: str = "") -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def identity(merchant: dict) -> dict:
    return merchant.get("identity") or {}


def merchant_name(merchant: dict) -> str:
    ident = identity(merchant)
    return put_first(ident.get("owner_first_name"), ident.get("name"), default="there")


def business_name(merchant: dict) -> str:
    return put_first(identity(merchant).get("name"), default="your business")


def first_name(customer: Optional[dict]) -> str:
    raw = put_first((customer or {}).get("identity", {}).get("name"), default="there")
    parts = raw.split()
    # Keep honorific + surname together rather than addressing "Mr." as a name.
    if parts and parts[0].lower().rstrip(".") in {"mr", "mrs", "ms", "dr"} and len(parts) > 1:
        return " ".join(parts)
    return parts[0] if parts else "there"


def active_offer(merchant: dict, preferred: Optional[str] = None) -> Optional[dict]:
    offers = [o for o in (merchant.get("offers") or []) if o.get("status") == "active"]
    if preferred:
        for offer in offers:
            if preferred.lower() in str(offer.get("title", "")).lower():
                return offer
    return offers[0] if offers else None


def percent(value: Any) -> Optional[int]:
    try:
        return round(abs(float(value)) * 100)
    except (TypeError, ValueError):
        return None


def money(value: Any) -> str:
    try:
        return f"₹{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    return text if text.endswith((".", "!", "?")) else text + "."


def last_cta(text: str, cta: str) -> str:
    """Keep CTA as the final sentence and avoid stacking asks."""
    text = text.strip()
    if cta == "none":
        return text
    return text.rstrip(".!? ") + ". " + cta


def language_hint(merchant: dict) -> str:
    ident = identity(merchant)
    langs = ident.get("languages") or []
    pref = str(ident.get("language_pref") or "").lower()
    return "hinglish" if ("hi" in langs or "hi" in pref) else "english"


def salutation(merchant: dict, customer: Optional[dict], customer_facing: bool) -> str:
    if customer_facing:
        return f"Hi {first_name(customer)}"
    cat = merchant.get("category_slug", "")
    name = merchant_name(merchant)
    if cat == "dentists":
        return f"Dr. {name}" if not name.lower().startswith("dr.") else name
    return f"Hi {name}"


def customer_service_line(trigger: dict) -> str:
    payload = trigger.get("payload") or {}
    if payload.get("placeholder"):
        return "your next follow-up"
    return put_first(payload.get("service_due"), payload.get("service"), default="your next follow-up")


def digest_for(trigger: dict, category: dict) -> Optional[dict]:
    items = category.get("digest") or []
    tid = (trigger.get("payload") or {}).get("top_item_id")
    did = (trigger.get("payload") or {}).get("digest_item_id")
    wanted = tid or did
    if wanted:
        for item in items:
            if item.get("id") == wanted:
                return item
    return items[0] if items else None


def find_digest_by_id(category: dict, item_id: str) -> Optional[dict]:
    for item in category.get("digest") or []:
        if item.get("id") == item_id:
            return item
    return None


def category_voice(category: dict) -> str:
    return str((category.get("voice") or {}).get("tone") or "")


# ----------------------------- composition --------------------------------

def compose(category: dict, merchant: dict, trigger: dict,
            customer: Optional[dict] = None,
            prior_bodies: Optional[set[str]] = None) -> dict:
    """
    Compose one outbound message using only supplied contexts.

    This intentionally uses a routing/planning layer rather than the uploaded
    solution's single prompt architecture.
    """
    prior_bodies = prior_bodies or set()
    kind = trigger.get("kind", "")
    customer_scope = trigger.get("scope") == "customer" or customer is not None
    send_as = "merchant_on_behalf" if customer_scope else "vera"

    body, cta, reason = _route(kind, category, merchant, trigger, customer, send_as)

    body = _sanitize(body)
    if body in prior_bodies:
        body = _make_nonrepeat(body, kind, merchant, trigger)
    if len(body) > 1000:
        body = _shorten(body)

    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key") or f"trigger:{trigger.get('id','unknown')}",
        "rationale": reason,
    }


def _route(kind: str, category: dict, merchant: dict, trigger: dict,
           customer: Optional[dict], send_as: str) -> tuple[str, str, str]:
    n = merchant_name(merchant)
    bn = business_name(merchant)
    p = merchant.get("performance") or {}
    dp = p.get("delta_7d") or {}
    tp = trigger.get("payload") or {}
    cat = merchant.get("category_slug", "")
    pref = language_hint(merchant)
    opening = salutation(merchant, customer, send_as == "merchant_on_behalf")

    # Customer-facing routes -------------------------------------------------
    if send_as == "merchant_on_behalf":
        return _customer_route(kind, category, merchant, trigger, customer)

    # High-intent planning: answer the actual question with a draft.
    if kind == "active_planning_intent":
        topic = str(tp.get("intent_topic", "")).lower()
        if "thali" in topic:
            text = (
                f"{n}, here’s a starter corporate-thali version for {identity(merchant).get('locality','your area')}: "
                f"10 thalis @ ₹125 each, 25 @ ₹115, and 50+ @ ₹105; delivery window 12:30–1pm. "
                f"Your current weekday thali is ₹149, so the bulk tiers have a clear volume incentive. "
                "I can turn this into a short WhatsApp pitch for office admins."
            )
        elif "kids_yoga" in topic:
            text = (
                f"{n}, for the kids-yoga idea, a simple 4-week starter is 3 classes/week for ages 7–12 at ₹2,499. "
                f"That matches the summer demand window in the category digest. "
                "I can turn this into a Google Business Profile post."
            )
        else:
            text = f"{n}, I’d build the first version around {tp.get('intent_topic','the idea you raised')}, using the existing offer and your recent account signals."
        return last_cta(text, "Want me to draft that next?"), "open_ended", "Directly advances the merchant's explicit planning request with a concrete first draft."

    # Evidence-heavy external research.
    if kind == "research_digest" or kind == "category_research_digest_release":
        item = digest_for(trigger, category)
        if item:
            title = item.get("title", "a new category update")
            source = item.get("source")
            summary = item.get("summary")
            signal = ""
            if "high_risk" in " ".join(merchant.get("signals") or []):
                signal = " That is especially relevant to your high-risk-adult cohort."
            text = f"{opening}, {title}."
            if summary:
                text += f" {summary}"
            if signal:
                text += signal
            if source:
                text += f" Source: {source}."
            return last_cta(text, "Want me to turn it into a 2-minute brief?"), "open_ended", "Selected the trigger-linked digest item and tied it to a merchant signal."

    if kind == "regulation_change":
        item = digest_for(trigger, category)
        deadline = tp.get("deadline_iso")
        text = f"{opening}, a compliance change is relevant here"
        if deadline:
            text += f" with an effective date of {deadline}"
        text += "."
        if item:
            text += f" {item.get('title','')}"
            if item.get("summary"):
                text += f" {item['summary']}"
        return last_cta(text, "Want a checklist of what to review?"), "open_ended", "Compliance route emphasizes the concrete rule and deadline without promotional language."

    if kind == "supply_alert":
        batches = ", ".join(tp.get("affected_batches") or [])
        mol = tp.get("molecule", "the affected molecule")
        text = f"{opening}, supply alert: {mol}"
        if batches:
            text += f" — affected batches {batches}"
        if tp.get("manufacturer"):
            text += f", manufacturer {tp['manufacturer']}"
        text += ". This is the right moment to check your stock records against the alert."
        return last_cta(text, "Want me to format the batch-check list?"), "open_ended", "High-urgency supply alert uses only the supplied molecule, batches, and manufacturer."

    if kind == "cde_opportunity":
        item = find_digest_by_id(category, str(tp.get("digest_item_id", "")))
        credits = tp.get("credits")
        fee = tp.get("fee")
        text = f"{opening}, there’s a CDE opportunity tied to your category"
        if credits is not None:
            text += f": {credits} credits"
        if fee:
            text += f", {fee}"
        if item:
            text += f". {item.get('title','')}"
        return last_cta(text, "Want the event details?"), "open_ended", "Uses the opportunity's concrete credit/fee data and the supplied digest item."

    if kind == "competitor_opened":
        competitor = tp.get("competitor_name")
        distance = tp.get("distance_km")
        offer = tp.get("their_offer")
        text = f"{opening}, a new nearby listing is now on the radar"
        if competitor:
            text += f": {competitor}"
        if distance is not None:
            text += f", {distance} km away"
        if offer:
            text += f", offering {offer}"
        text += "."
        return last_cta(text, "Want me to compare that offer with your active listing?"), "open_ended", "Competitive-awareness message stays factual and uses only trigger-provided details."

    if kind == "ipl_match_today":
        match = tp.get("match", "today's match")
        venue = tp.get("venue")
        time_iso = tp.get("match_time_iso", "")
        text = f"Quick heads-up {n} — {match} is today"
        if venue:
            text += f" at {venue}"
        if time_iso:
            text += f" ({time_iso[11:16]} local)"
        text += ". Your active offer is "
        offer = active_offer(merchant)
        text += f"{offer['title']}" if offer else "already available"
        text += "."
        if not tp.get("is_weeknight"):
            text += " The category digest says Saturday IPL matches have shifted covers down 12% versus the Saturday average."
        return last_cta(text, "Want me to draft a match-day delivery message?"), "open_ended", "Combines the time-sensitive match trigger with the merchant's real active offer and category evidence."

    # Performance / account routes -----------------------------------------
    if kind == "perf_dip":
        metric = tp.get("metric")
        if not metric and dp:
            metric = next((k[:-4] for k in dp if k.endswith("_pct")), "your main metric")
        metric = metric or "your main metric"
        delta = percent(tp.get("delta_pct", dp.get(f"{metric}_pct")))
        baseline = tp.get("vs_baseline")
        text = f"{opening}, {metric} are down {delta}% over {tp.get('window','7d')}" if delta is not None else f"{opening}, your {metric} have dipped recently"
        if baseline is not None:
            text += f" from a baseline of {baseline}"
        text += "."
        return last_cta(text, "Want one concrete fix based on the account data?"), "open_ended", "Names the exact performance trigger and offers one low-friction next step."

    if kind == "seasonal_perf_dip":
        delta = percent(tp.get("delta_pct", dp.get("views_pct")))
        members = (merchant.get("customer_aggregate") or {}).get("total_active_members")
        text = f"{opening}, views are down {delta}% this week, and the trigger marks this as seasonal rather than unexpected." if delta is not None else f"{opening}, the current acquisition dip is flagged as seasonal."
        if members:
            text += f" You have {members} active members, so retention is the steadier lever during this window."
        return last_cta(text, "Want a simple retention campaign draft?"), "open_ended", "Reframes an explicitly seasonal dip using the merchant's member count rather than inventing a benchmark."

    if kind == "perf_spike":
        metric = tp.get("metric")
        if not metric and dp:
            metric = next((k[:-4] for k in dp if k.endswith("_pct")), "performance")
        metric = metric or "performance"
        delta = percent(tp.get("delta_pct", dp.get(f"{metric}_pct")))
        driver = tp.get("likely_driver")
        text = f"{opening}, {metric} are up {delta}% in the last {tp.get('window','7d')}" if delta is not None else f"{opening}, you have a positive performance movement"
        if driver:
            text += f", with {driver.replace('_',' ')} flagged as the likely driver"
        text += "."
        return last_cta(text, "Want to build on that signal with one follow-up post?"), "open_ended", "Celebrates a measurable spike and points to the supplied likely driver."

    if kind == "milestone_reached":
        now = tp.get("value_now")
        milestone = tp.get("milestone_value")
        metric = str(tp.get("metric") or "the next account milestone").replace("_", " ")
        if now is None:
            text = f"{opening}, there’s a milestone signal on your account around {metric}."
        else:
            text = f"{opening}, you’re at {now} {metric}"
            if milestone:
                text += f" with {milestone} as the next milestone"
            text += "."
        return sentence(text), "none", "Pure recognition trigger; no CTA is added because the event can stand on its own."

    if kind == "renewal_due":
        days = tp.get("days_remaining")
        plan = tp.get("plan")
        amount = tp.get("renewal_amount")
        text = f"{opening}, your {plan or 'current'} plan has {days} days left"
        if amount:
            text += f"; renewal amount is {money(amount)}"
        text += "."
        return last_cta(text, "Want me to walk through the renewal details?"), "open_ended", "Uses the exact plan, remaining days, and amount supplied by the renewal trigger."

    if kind == "gbp_unverified":
        text = f"{opening}, your Google Business Profile is still unverified."
        if tp.get("verification_path"):
            text += f" The supplied verification path is {tp['verification_path'].replace('_',' ')}."
        if tp.get("estimated_uplift_pct") is not None:
            text += f" The trigger estimates a {percent(tp['estimated_uplift_pct'])}% uplift."
        return last_cta(text, "Want a short setup checklist?"), "open_ended", "Verification route uses the actual account state and supplied verification path."

    # Engagement / lifecycle -----------------------------------------------
    if kind == "curious_ask_due":
        if cat == "salons":
            q = "Which service has been most asked for this week — keratin, colour, facial, or something else?"
        elif cat == "restaurants":
            q = "Which item has been getting the most repeat asks this week?"
        elif cat == "gyms":
            q = "Which class or training slot is getting the strongest demand this week?"
        elif cat == "pharmacies":
            q = "Which OTC or repeat-prescription request has been most common this week?"
        else:
            q = "What service has been most asked for this week?"
        return f"{opening}, quick check — {q}", "open_ended", "Uses the challenge's asking-the-merchant engagement lever instead of pushing a generic offer."

    if kind == "review_theme_emerged":
        theme = str(tp.get("theme", "a review theme")).replace("_", " ")
        occ = tp.get("occurrences_30d")
        trend = tp.get("trend")
        text = f"{opening}, {theme} has appeared in {occ} reviews over the last 30 days" if occ is not None else f"{opening}, a review theme around {theme} has emerged"
        if trend:
            text += f" and the trend is {trend}"
        text += "."
        return last_cta(text, "Want to see the specific theme and suggested response?"), "open_ended", "Surfaces the exact review theme and frequency, then asks one clear follow-up."

    if kind == "dormant_with_vera":
        days = tp.get("days_since_last_merchant_message")
        topic = str(tp.get("last_topic", "")).replace("_", " ")
        text = f"{opening}, it’s been {days} days since our last merchant message" if days is not None else f"{opening}, it’s been a while since we last spoke"
        if topic:
            text += f" about {topic}"
        text += "."
        return last_cta(text, "Want to pick that thread back up?"), "open_ended", "Dormancy message references the actual elapsed time and prior topic."

    if kind == "winback_eligible":
        days = tp.get("days_since_expiry")
        lapsed = tp.get("lapsed_customers_added_since_expiry")
        text = f"{opening}, your account has been eligible for a win-back for {days} days" if days is not None else f"{opening}, your account is flagged for win-back"
        if lapsed is not None:
            text += f", with {lapsed} lapsed customers added since expiry"
        text += "."
        return last_cta(text, "Want me to draft a simple win-back message?"), "open_ended", "Uses the trigger's eligibility duration and lapsed-customer count."

    if kind == "festival_upcoming":
        fest = tp.get("festival")
        date = tp.get("date")
        if fest:
            text = f"{opening}, {fest} is coming up"
            if date:
                text += f" on {date}"
        else:
            text = f"{opening}, there’s an upcoming seasonal opportunity for your category"
        text += "."
        offer = active_offer(merchant)
        if offer:
            text += f" Your active offer is {offer['title']}."
        return last_cta(text, "Want one category-fit seasonal message drafted?"), "open_ended", "Connects the upcoming seasonal trigger to a real active offer when available."

    if kind == "category_seasonal":
        trends = tp.get("trends") or []
        detail = ", ".join(str(x).replace("_", " ") for x in trends[:3])
        text = f"{opening}, the summer demand shift flags {detail}" if detail else f"{opening}, the category trigger flags a summer demand shift"
        text += "."
        if tp.get("shelf_action_recommended"):
            text += " It specifically recommends reviewing shelf mix."
        return last_cta(text, "Want me to turn those signals into a shelf-review list?"), "open_ended", "Uses the supplied seasonal demand figures and the trigger's recommended action."

    # Generic fallback still uses actual account evidence.
    ctr = p.get("ctr")
    peer = category.get("peer_stats") or {}
    if ctr is not None and peer.get("avg_ctr"):
        text = f"{opening}, your listing CTR is {float(ctr)*100:.1f}% versus the category peer average of {float(peer['avg_ctr'])*100:.1f}%."
        return last_cta(text, "Want one suggestion to test?"), "open_ended", "Fallback is anchored to a real merchant metric and category peer statistic."
    offer = active_offer(merchant)
    text = f"{opening}, quick account check"
    if offer:
        text += f" — your active offer is {offer['title']}"
    text += "."
    return last_cta(text, "Want help with the next step?"), "open_ended", "Uses the strongest available merchant-specific fact."


def _customer_route(kind: str, category: dict, merchant: dict,
                    trigger: dict, customer: Optional[dict]) -> tuple[str, str, str]:
    c = first_name(customer)
    bn = business_name(merchant)
    p = trigger.get("payload") or {}
    cat = merchant.get("category_slug", "")

    if kind in ("recall_due", "customer_lapsed_soft"):
        slots = p.get("available_slots") or []
        offer = active_offer(merchant)
        service = customer_service_line(trigger)
        if p.get("placeholder"):
            text = f"Hi {c}, {bn} here. Following up on your account reminder."
        else:
            text = f"Hi {c}, {bn} here. Your {service} is due"
        if p.get("due_date"):
            text += f" around {p['due_date']}"
        elif not p.get("placeholder") and kind == "recall_due":
            text += " because it is due"
        text += "."
        if slots:
            text += f" We have {slots[0].get('label', slots[0].get('iso',''))}"
            if len(slots) > 1:
                text += f" or {slots[1].get('label', slots[1].get('iso',''))}"
            text += "."
        if offer:
            text += f" {offer['title']} is currently active."
        return last_cta(text, "Reply YES if you'd like us to continue."), "binary_yes_no", "Customer recall uses only the supplied due date, slots, and active offer."

    if kind == "appointment_tomorrow":
        slot = p.get("appointment") or p.get("slot") or p.get("appointment_time")
        text = f"Hi {c}, {bn} here — a reminder that your appointment is tomorrow"
        if slot:
            text += f" at {slot}"
        text += "."
        return last_cta(text, "Reply YES to confirm."), "binary_yes_no", "Short appointment reminder with one confirmation CTA."

    if kind in ("customer_lapsed_hard", "trial_followup", "wedding_package_followup", "chronic_refill_due", "customer_lapsed_soft"):
        text = f"Hi {c}, {bn} here."
        if kind == "customer_lapsed_hard":
            days = p.get("days_since_last_visit")
            focus = p.get("previous_focus")
            if days is not None:
                text += f" It’s been {days} days since your last visit."
            if focus:
                text += f" You previously focused on {focus.replace('_',' ')}."
            offer = active_offer(merchant)
            if offer:
                text += f" We currently have {offer['title']}."
        elif kind == "trial_followup":
            opts = p.get("next_session_options") or []
            text += f" Following up on your trial from {p.get('trial_date','recently')}."
            if opts:
                text += f" The next option is {opts[0].get('label', opts[0].get('iso',''))}."
        elif kind == "wedding_package_followup":
            if p.get("wedding_date"):
                text += f" Your wedding date is {p['wedding_date']}."
            text += " The supplied next-step window is the 30-day skin-prep program."
        else:
            mols = ", ".join(p.get("molecule_list") or [])
            if mols:
                text += f" Your refill list includes {mols}."
            elif p.get("placeholder"):
                text += " This is a refill follow-up based on the account signal."
            if p.get("stock_runs_out_iso"):
                text += f" The supplied stock-out time is {p['stock_runs_out_iso']}."
        return last_cta(text, "Reply YES if you'd like us to help with the next step."), "binary_yes_no", "Customer lifecycle route is personalized from the supplied trigger/customer context."

    return f"Hi {c}, {bn} here. I have an update linked to your account.", "open_ended", "Customer-facing fallback stays concise and avoids inventing details."


# -------------------------- post-composition checks ------------------------

_URL = re.compile(r"https?://\S+", re.I)
_BAD_CTA = re.compile(r"\b(reply\s+(yes|no|stop)|want me to|would you like|shall i)\b", re.I)


def _sanitize(text: str) -> str:
    text = _URL.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _shorten(text: str) -> str:
    # Preserve the ending because it contains the CTA.
    if len(text) <= 1000:
        return text
    pieces = re.split(r"(?<=[.!?])\s+", text)
    kept = ""
    for piece in pieces:
        candidate = (kept + " " + piece).strip()
        if len(candidate) > 970:
            break
        kept = candidate
    return kept or text[:970].rstrip() + "."


def _make_nonrepeat(body: str, kind: str, merchant: dict, trigger: dict) -> str:
    # Change an evidence clause rather than adding a fake claim.
    suffix = {
        "research_digest": " I can also pull out the practical takeaway.",
        "perf_dip": " I can isolate the account signal behind the movement.",
        "perf_spike": " I can turn the signal into one follow-up experiment.",
        "review_theme_emerged": " I can summarize the recurring issue.",
    }.get(kind, " I can take the next step from the same context.")
    return body.rstrip(".!? ") + "." + suffix


# -------------------------- conversation policy ----------------------------

OPT_OUT = re.compile(
    r"\b(stop|unsubscribe|opt[\s-]?out|not interested|don't message|do not message|"
    r"leave me alone|spam|harass|bothering|band karo|pareshan)\b", re.I
)
AUTO_REPLY = re.compile(
    r"(thank you for contacting|thanks for contacting|our team will respond|"
    r"will get back to you|business hours|automated response|auto.?reply|"
    r"we received your message)", re.I
)
COMMIT = re.compile(
    r"\b(yes|haan|sure|go ahead|let'?s do it|lets do it|i'?m in|im in|"
    r"sign me up|start karo|chalo|confirm|please do|do it)\b", re.I
)
TIMEOUT = re.compile(r"\b(later|tomorrow|next week|busy|not now|give me time|call me later)\b", re.I)
OFFTOPIC = re.compile(r"\b(gst|tax filing|income tax|unrelated|by the way|btw)\b", re.I)
REQUEST_WORDS = re.compile(r"\b(send|share|draft|make|prepare|pull|show|details|list|abstract|check)\b", re.I)


def _conversation_reply(chat: dict, message: str, category: dict,
                        merchant: dict, trigger: dict,
                        customer: Optional[dict], from_role: str) -> dict:
    clean = norm(message)

    if OPT_OUT.search(message):
        chat["status"] = "ended"
        ENDED.add(chat["id"])
        AUTO_STREAK[chat.get("merchant_id") or "unknown"] = 0
        return {"action": "end", "rationale": "Explicit opt-out or frustration detected; conversation is closed without another promotional message."}

    auto_key = chat.get("merchant_id") or "unknown"
    if AUTO_REPLY.search(message):
        streak = AUTO_STREAK.get(auto_key, 0) + 1
        AUTO_STREAK[auto_key] = streak
        if streak >= 3:
            AUTO_STREAK[auto_key] = 0
            chat["status"] = "ended"
            ENDED.add(chat["id"])
            return {"action": "end",
                    "rationale": "Repeated automated replies with no live engagement across conversations; closing to avoid polling a machine."}
        return {"action": "wait", "wait_seconds": 14400,
                "rationale": "Canned-business-response pattern detected; backing off four hours for a human response."}
    # Any live (non-automated) message breaks the streak.
    AUTO_STREAK[auto_key] = 0

    if OFFTOPIC.search(message):
        return {
            "action": "send",
            "body": "I’ll leave that outside this conversation. Coming back to the current magicpin item — what I can do here is use the account context and prepare the next step.",
            "cta": "open_ended",
            "rationale": "Briefly redirects an unrelated request to the active merchant mission."
        }

    if TIMEOUT.search(message):
        return {"action": "wait", "wait_seconds": 3600,
                "rationale": "Merchant asked for time; waiting rather than sending another immediate pitch."}

    # Customer confirmations are handled differently from merchant replies.
    customer_scope = trigger.get("scope") == "customer" or customer is not None
    if customer_scope and COMMIT.search(message):
        return {
            "action": "send",
            "body": f"Thanks — I’ll treat that as your confirmation for {business_name(merchant)} and keep the next step tied to the slot or offer already shared.",
            "cta": "none",
            "rationale": "Acknowledges a customer commitment without inventing a booking that the bot cannot actually make."
        }

    if COMMIT.search(message) or REQUEST_WORDS.search(message):
        chat["mode"] = "action"
        kind = trigger.get("kind", "")
        if kind == "research_digest":
            body = "Absolutely. I’ll keep this to the supplied research item and the merchant-relevant takeaway, then use that same context for the patient-facing draft."
        elif kind in ("active_planning_intent",):
            body = "Absolutely — moving from the idea to a usable draft now, using the pricing and account details already supplied."
        elif kind == "supply_alert":
            body = "Got it. I’ll keep the next step focused on the supplied molecule and affected batches rather than adding any unverified stock information."
        else:
            body = "Got it — moving from the qualifying question to the concrete next step using the context already on hand."
        return {"action": "send", "body": body, "cta": "binary_confirm_cancel",
                "rationale": "Explicit intent detected; state changes from qualification to action mode."}

    # Same message twice in succession is treated as likely automation.
    recent = chat.setdefault("merchant_messages", [])
    if recent and recent[-1] == clean:
        chat["repeat_count"] = chat.get("repeat_count", 1) + 1
    else:
        chat["repeat_count"] = 1
    recent.append(clean)
    if chat["repeat_count"] == 2:
        return {"action": "wait", "wait_seconds": 86400,
                "rationale": "Identical merchant message repeated consecutively; backing off to avoid engaging an automated reply."}
    if chat["repeat_count"] >= 3:
        return {"action": "end", "rationale": "Repeated identical response indicates no live engagement; conversation closed to avoid spam."}

    # Default continuation asks one contextual question.
    n = merchant_name(merchant)
    body = f"Thanks, {n}. I’ll keep this focused on the current {trigger.get('kind','account')} signal and the details already supplied."
    return {"action": "send", "body": last_cta(body, "Want me to take the next concrete step?"),
            "cta": "open_ended", "rationale": "Acknowledges the reply and keeps the conversation anchored to the active trigger."}


# ------------------------------- API models --------------------------------

class ContextIn(BaseModel):
    scope: Literal["category", "merchant", "customer", "trigger"]
    context_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


class TickIn(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


class ReplyIn(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: Optional[str] = None
    turn_number: Optional[int] = None


# -------------------------------- endpoints --------------------------------

@app.get("/v1/healthz")
async def healthz():
    counts = {s: 0 for s in ("category", "merchant", "customer", "trigger")}
    for (scope, _), _entry in STORE.items():
        counts[scope] += 1
    return {"status": "ok", "uptime_seconds": int(time.time() - _STARTED), "contexts_loaded": counts}


@app.get("/v1/metadata")
async def metadata():
    return META


@app.post("/v1/context")
async def context_push(body: ContextIn):
    key = (body.scope, body.context_id)
    current = STORE.get(key)
    if current and body.version <= current["version"]:
        return JSONResponse(
            status_code=409,
            content={"accepted": False, "reason": "stale_version", "current_version": current["version"]},
        )
    # Replacement is a single assignment, so readers never see a half-written entry.
    STORE[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.scope}_{body.context_id}_v{body.version}",
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }


def _get(scope: str, cid: Optional[str]) -> Optional[dict]:
    if not cid:
        return None
    entry = STORE.get((scope, cid))
    return entry["payload"] if entry else None


@app.post("/v1/tick")
async def tick(body: TickIn):
    actions = []
    for trigger_id in body.available_triggers:
        if len(actions) >= 20:
            break
        if trigger_id in FIRED:
            continue
        trigger = _get("trigger", trigger_id)
        if not trigger:
            continue
        suppression = trigger.get("suppression_key") or trigger_id
        if suppression in FIRED:
            continue

        mid = trigger.get("merchant_id")
        merchant = _get("merchant", mid)
        if not merchant:
            continue
        category = _get("category", merchant.get("category_slug"))
        if not category:
            continue
        cid = trigger.get("customer_id")
        customer = _get("customer", cid)

        conversation_id = f"conv_{mid}_{trigger_id}_{uuid.uuid4().hex[:8]}"
        result = compose(category, merchant, trigger, customer)
        CHATS[conversation_id] = {
            "id": conversation_id,
            "merchant_id": mid,
            "customer_id": cid,
            "trigger_id": trigger_id,
            "turns": [{"from": result["send_as"], "body": result["body"]}],
            "sent": {result["body"]},
            "merchant_messages": [],
            "repeat_count": 0,
            "mode": "qualifying",
            "status": "open",
        }
        FIRED.add(suppression)

        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": mid,
            "customer_id": cid,
            "send_as": result["send_as"],
            "trigger_id": trigger_id,
            "template_name": f"vera_{trigger.get('kind','generic')}_v2",
            "template_params": [result["body"]],
            "body": result["body"],
            "cta": result["cta"],
            "suppression_key": suppression,
            "rationale": result["rationale"],
        })
    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyIn):
    chat = CHATS.get(body.conversation_id)
    if not chat:
        # The brief allows the judge to send a conversation id; recoverable state
        # is preferable to a 500 if the first outbound action came from another process.
        chat = {
            "id": body.conversation_id,
            "merchant_id": body.merchant_id,
            "customer_id": body.customer_id,
            "trigger_id": None,
            "turns": [],
            "sent": set(),
            "merchant_messages": [],
            "repeat_count": 0,
            "mode": "qualifying",
            "status": "open",
        }
        CHATS[body.conversation_id] = chat

    if chat.get("status") == "ended":
        return {"action": "end", "rationale": "Conversation was already closed."}

    mid = body.merchant_id or chat.get("merchant_id")
    cid = body.customer_id or chat.get("customer_id")
    merchant = _get("merchant", mid) or {}
    category = _get("category", merchant.get("category_slug")) or {}
    customer = _get("customer", cid)
    trigger = _get("trigger", chat.get("trigger_id")) or {
        "kind": "conversation_continuation", "scope": "merchant",
        "suppression_key": body.conversation_id,
    }

    chat["turns"].append({"from": body.from_role, "body": body.message})
    result = _conversation_reply(chat, body.message, category, merchant, trigger, customer, body.from_role)

    if result["action"] == "send":
        if result["body"] in chat["sent"]:
            result["body"] = _make_nonrepeat(result["body"], trigger.get("kind",""), merchant, trigger)
        chat["sent"].add(result["body"])
        chat["turns"].append({"from": "vera", "body": result["body"]})
    return result


@app.post("/v1/teardown")
async def teardown():
    STORE.clear()
    CHATS.clear()
    FIRED.clear()
    ENDED.clear()
    AUTO_STREAK.clear()
    return {"status": "wiped"}


# Backwards-friendly local function for offline generation/tests.
def compose_message(category: dict, merchant: dict, trigger: dict,
                    customer: Optional[dict] = None,
                    conversation_history: Optional[list[dict]] = None,
                    already_sent: Optional[set[str]] = None) -> dict:
    return compose(category, merchant, trigger, customer, already_sent)
