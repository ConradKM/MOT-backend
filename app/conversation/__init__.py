"""CoMaz OS's provider-independent conversation/booking engine - the "brain"
that will eventually sit behind Twilio Voice and WhatsApp (and, later, an
LLM front-end).

Nothing in this package imports ``twilio`` or knows about webhooks - it
accepts plain (garage, channel, phone, text) input and returns a plain
result (see engine.py::handle_message). app/communications/whatsapp_webhooks.py
and the development simulator (routes.py) are the only two callers; both are
thin adapters that translate their own transport into that call.

Design rules this package is built around (see the project brief this
implements for the full rationale):

* The engine never invents availability, prices, appointment types, or
  booking data - every fact comes from a real query in actions.py, which is
  the *only* module allowed to touch booking/customer/vehicle/availability
  models directly.
* Session state (models/conversation/conversation_session.py) is explicit,
  structured, JSON-safe data - never opaque "AI reasoning".
* Intent detection (intents.py) is a swappable strategy
  (``IntentResolver``) - today's ``RuleBasedIntentResolver`` needs no LLM;
  a future NLU-backed resolver can replace it without changing anything
  downstream, because everything downstream only ever sees a plain intent
  string plus validated, typed slot values.
"""
