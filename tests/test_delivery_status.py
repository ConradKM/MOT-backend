"""Unit tests for app/communications/delivery_status.py - the Twilio
status/error-code -> business-facing text mapping."""

from app.communications.delivery_status import (
    delivery_status_label,
    describe_delivery_failure,
    template_required,
)


def test_24h_window_code_explains_the_template_requirement():
    msg = describe_delivery_failure("63016", "undelivered")
    assert "24 hours" in msg
    assert "template" in msg.lower()
    assert template_required("63016") is True


def test_recipient_not_on_whatsapp_is_explained():
    assert "not reachable on WhatsApp" in describe_delivery_failure(63003, "undelivered")
    assert template_required(63003) is False


def test_unknown_failure_code_still_gives_an_honest_line_not_none():
    msg = describe_delivery_failure("64999", "failed")
    assert msg is not None
    assert "64999" in msg


def test_failure_with_no_code_is_generic_but_not_misleading():
    assert describe_delivery_failure(None, "undelivered") == (
        "Not delivered — the messaging provider did not accept it."
    )


def test_in_flight_status_has_nothing_to_explain():
    assert describe_delivery_failure(None, "queued") is None
    assert describe_delivery_failure("", "sent") is None


def test_status_label_is_business_facing():
    assert delivery_status_label("delivered") == "Delivered"
    assert delivery_status_label("queued") == "Queued"
    assert delivery_status_label("SKIPPED_NOT_CONFIGURED").startswith("Not sent")
    # A failure always reads as "Not delivered …", never the bare provider word.
    assert delivery_status_label("undelivered", "63016").startswith("Not delivered —")
    assert "24 hours" in delivery_status_label("undelivered", "63016")
    assert delivery_status_label("failed", None).startswith("Not delivered")
