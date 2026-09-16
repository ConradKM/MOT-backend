"""OpenAI Realtime voice AI for inbound calls - an alternative to
app/communications/voice_relay.py's ConversationRelay path, not layered on
top of it. See docs/OPENAI_VOICE.md for the architecture.

- config.py: the OPENAI_VOICE_ENABLED gate and other settings.
- instructions.py: builds a per-business system prompt from real CoMaz data.
- tools.py: the tool/function schemas the model can call, and the tenant-
  scoped dispatcher that actually executes one.
- realtime_bridge.py: the outbound WebSocket client to OpenAI's Realtime API.
- twiml.py: the ``<Connect><Stream>`` TwiML for an inbound call.

The bridge itself (Twilio Media Streams <-> this package) lives in
app/ws/openai_voice.py, mirroring app/ws/twilio_voice.py's shape.
"""
