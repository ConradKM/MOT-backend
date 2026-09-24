"""A business's configurable incoming-call menu (IVR).

- actions.py: the extensible action registry and option validation
- service.py: load / validate / save a business's menu
- twiml.py: the TwiML for each step of an inbound call
- routes.py: the owner-facing settings API (Settings > Phone menu)

The Twilio webhooks that drive a live call live alongside the existing
inbound voice webhook in app/communications/voice_webhooks.py.
"""
