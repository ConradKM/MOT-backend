import logging
import os
import sys
import uuid

from flask import Flask, request
from flask_cors import CORS

from .config import Config
from .extensions import api, db, jwt, limiter, migrate, sock
from .platform_admin.impersonation import CLAIM_SESSION_ID as IMPERSONATION_CLAIM
from .platform_admin.security import ACCOUNT_TYPE_PLATFORM_ADMIN


def _configure_logging(app: Flask) -> None:
    """Send application logs to stdout at a configurable level so they show up
    in the platform log stream (Render). Without this, Flask's logger falls
    back to ``logging.lastResort`` which drops everything below WARNING - so
    ``logger.info`` diagnostics (e.g. the VOICE_* markers in
    app/ws/twilio_voice.py) never appear in production. No-op under tests."""
    if app.testing:
        return
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    if not any(getattr(h, "_comaz_stdout", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler._comaz_stdout = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
    app.logger.setLevel(level)


def _log_validation_errors(app: Flask) -> None:
    """Log the field-by-field reason behind every 4xx that carries
    flask-smorest's own ``errors`` body (a raw marshmallow/webargs schema
    validation failure - an unknown field, an out-of-range value, a bad
    enum), so a request like ``POST /tenants`` failing with a 422 leaves more
    in the platform log stream than a bare access-log line with no body.

    Deliberately safe to log broadly: ``errors`` is marshmallow's own
    {field: [message, ...]} structure - field *names* and canned validation
    *messages* only, never the submitted values (so nothing a client typed,
    and therefore nothing secret, ever reaches this log line) and never an
    application-level ``abort(422, message=...)`` body (those carry
    `message`, not `errors`, and are a deliberate business decision already
    visible in the response - not a mystery to diagnose).
    """

    @app.after_request
    def _log(response):
        if 400 <= response.status_code < 500 and response.is_json:
            body = response.get_json(silent=True)
            if isinstance(body, dict) and body.get("errors"):
                app.logger.warning(
                    "[validation] %s %s -> %s errors=%s",
                    request.method,
                    request.path,
                    response.status_code,
                    body["errors"],
                )
        return response


@jwt.token_in_blocklist_loader
def _token_revoked(_jwt_header, jwt_payload) -> bool:
    """Decide whether a decoded JWT is still usable, for every account type.

    Runs on every authenticated request, which is what makes the checks below
    take effect *immediately* rather than at token expiry:

    * customer-portal tokens (``account_type == "customer"``) are left to
      app/customer_auth, exactly as before;
    * platform-admin tokens are re-resolved against ``platform_admins``
      (app/platform_admin/security.py) - a deactivated admin's open tab stops
      working on its next click;
    * an employee token is rejected if the account is gone, deactivated, or
      predates the user's last password reset;
    * an employee token carrying impersonation claims additionally dies the
      moment its :class:`ImpersonationSession` is revoked or expires; and
    * an ordinary employee token is rejected while its tenant is suspended.

    Impersonation is deliberately exempt from the suspension check: entering a
    suspended tenant to fix whatever caused the suspension is the whole point
    of support access, and that path is short-lived and audited.
    """
    account_type = jwt_payload.get("account_type")

    if account_type == "customer":
        return False

    if account_type == ACCOUNT_TYPE_PLATFORM_ADMIN:
        from .platform_admin.security import platform_admin_token_revoked

        return platform_admin_token_revoked(jwt_payload)

    from sqlalchemy.orm import joinedload

    from .models.employee import Employee
    from .models.garage import GARAGE_STATUS_SUSPENDED

    identity = jwt_payload.get("sub")
    try:
        # The garage is eager-loaded because the suspension check below needs
        # it - one query per request, the same as before this check existed.
        employee = db.session.get(
            Employee, uuid.UUID(identity), options=[joinedload(Employee.garage)]
        )
    except (TypeError, ValueError):
        return True

    if employee is None or not employee.is_active:
        return True

    valid_from = employee.tokens_valid_from
    # `iat` is integer seconds (RFC 7519) while tokens_valid_from carries
    # microseconds, so comparing them raw rejects a token minted in the very
    # second of the invalidation - which is precisely the token a "set your
    # password, then sign in" flow issues. Floor both to the second: anything
    # issued before that second is still rejected.
    if valid_from is not None and jwt_payload.get("iat", 0) < int(valid_from.timestamp()):
        return True

    if jwt_payload.get(IMPERSONATION_CLAIM) is not None:
        from .platform_admin.impersonation import impersonation_token_revoked

        return impersonation_token_revoked(jwt_payload)

    return employee.garage is not None and employee.garage.status == GARAGE_STATUS_SUSPENDED


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    _configure_logging(app)
    _log_validation_errors(app)

    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    limiter.init_app(app)
    api.init_app(app)
    sock.init_app(app)

    # Cross-origin access to the API for deployed static frontends (the local
    # dev frontend proxies same-origin and needs none). Explicit allowlist from
    # config - never "*", since every request carries an Authorization bearer.
    CORS(
        app,
        resources={r"/api/*": {"origins": list(app.config["CORS_ORIGINS"])}},
        allow_headers=["Authorization", "Content-Type"],
        methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        supports_credentials=False,
        max_age=3600,
    )

    api.spec.components.security_scheme(
        "bearerAuth", {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    )

    from .communications.cli import (
        configure_garage_communications_command,
        set_conversation_automation_command,
        twilio_webhook_urls_command,
    )
    from .dev.cli import dev_info_command, seed_dev_command
    from .garages.cli import onboard_garage_command, update_garage_details_command
    from .platform_admin.cli import (
        create_platform_admin_command,
        list_platform_admins_command,
    )

    app.cli.add_command(create_platform_admin_command)
    app.cli.add_command(list_platform_admins_command)
    app.cli.add_command(onboard_garage_command)
    app.cli.add_command(update_garage_details_command)
    app.cli.add_command(configure_garage_communications_command)
    app.cli.add_command(set_conversation_automation_command)
    app.cli.add_command(twilio_webhook_urls_command)
    app.cli.add_command(seed_dev_command)
    app.cli.add_command(dev_info_command)

    from .appointments.checklist_templates.routes import checklist_templates_blp
    from .appointments.checklists.routes import appointment_checklists_blp
    from .appointments.media.routes import checklist_item_media_blp
    from .appointments.routes import appointments_blp
    from .appointments.statuses.routes import appointment_statuses_blp
    from .appointments.types.routes import appointment_types_blp
    from .auth.routes import auth_blp
    from .booking_requests.routes import booking_requests_blp
    from .communications.routes import communications_blp
    from .communications.voice_webhooks import twilio_voice_blp
    from .communications.whatsapp_webhooks import twilio_whatsapp_blp
    from .conversation.routes import conversation_blp
    from .customer_auth.routes import customer_auth_blp
    from .customer_portal.routes import customer_portal_blp
    from .customers.routes import customers_blp
    from .employees.routes import employees_blp
    from .garages.routes import garages_blp, public_garages_blp
    from .garages.schedule.routes import garage_schedule_blp
    from .health.routes import health_blp
    from .mot_records.routes import mot_records_blp
    from .mot_reminders.routes import mot_reminders_blp
    from .platform_admin.routes import PLATFORM_ADMIN_BLUEPRINTS
    from .public_booking.routes import public_booking_blp
    from .roles.routes import roles_blp
    from .vehicles.routes import vehicles_blp

    api.register_blueprint(health_blp)
    api.register_blueprint(twilio_voice_blp)
    api.register_blueprint(twilio_whatsapp_blp)
    api.register_blueprint(auth_blp)
    api.register_blueprint(customer_auth_blp)
    api.register_blueprint(customer_portal_blp)
    api.register_blueprint(garages_blp)
    api.register_blueprint(garage_schedule_blp)
    api.register_blueprint(public_garages_blp)
    api.register_blueprint(public_booking_blp)
    api.register_blueprint(booking_requests_blp)
    api.register_blueprint(communications_blp)
    api.register_blueprint(conversation_blp)
    api.register_blueprint(customers_blp)
    api.register_blueprint(employees_blp)
    api.register_blueprint(roles_blp)
    api.register_blueprint(vehicles_blp)
    api.register_blueprint(mot_records_blp)
    api.register_blueprint(mot_reminders_blp)
    api.register_blueprint(appointment_types_blp)
    api.register_blueprint(appointment_statuses_blp)
    api.register_blueprint(checklist_templates_blp)
    api.register_blueprint(appointments_blp)
    api.register_blueprint(appointment_checklists_blp)
    api.register_blueprint(checklist_item_media_blp)

    # The internal operator console (app/platform_admin). A separate namespace
    # (/api/platform-admin/*) behind a separate account table - no garage or
    # customer credential reaches any of it.
    for platform_blp in PLATFORM_ADMIN_BLUEPRINTS:
        api.register_blueprint(platform_blp)

    # Importing app.ws registers its @sock.route handlers on the sock instance
    # init'd above (served only under a WebSocket-capable server - gunicorn's
    # gevent worker in production).
    from . import ws  # noqa: F401
    from .conversation.automation import register_default_handlers
    from .email.automation import register_email_handlers
    from .models import (  # noqa: F401
        booking_request,
        customer,
        employee,
        garage,
        garage_schedule,
        mot_record,
        mot_reminder_settings,
        password_reset_token,
        reminder,
        role,
        vehicle,
    )
    from .models.appointments import (  # noqa: F401
        appointment,
        appointment_checklist,
        appointment_checklist_item,
        appointment_status,
        appointment_type,
        checklist_item_media,
        checklist_template,
        checklist_template_item,
    )
    from .models.communications import (  # noqa: F401
        automation_settings,
        comms_onboarding,
        communication_log,
        conversation_state,
        garage_communication_settings,
        message_template,
    )
    from .models.conversation import callback_request, conversation_session  # noqa: F401
    from .models.platform import (  # noqa: F401
        admin,
        audit_log,
        feature_flag,
        impersonation,
    )

    register_default_handlers()
    register_email_handlers()

    return app
