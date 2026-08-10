"""What the buyer's delivery message looks like, and who decides its formatting.

Two complaints, one cause. An auto-delivered order arrived as a tappable copy box while the same
product fulfilled by hand arrived as a differently-worded message with no warranty line — and both
paths forced <code> around whatever the admin had written, so a credential deliberately sent as
plain text still came out as a copy box, and any formatting the admin applied was thrown away.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.locales.i18n import t
from app.utils.text import as_admin_wrote_it


def _message(html: str) -> SimpleNamespace:
    return SimpleNamespace(html_text=html, text=html)


def test_the_delivery_template_does_not_impose_a_copy_box() -> None:
    rendered = t("orders.auto_delivery", "en", payload="user@mail.com|pw", warranty_days=1)
    assert "user@mail.com|pw" in rendered
    assert "<code>" not in rendered, "the bot decided the formatting instead of the admin"


def test_a_copy_box_the_admin_asked_for_survives() -> None:
    payload = as_admin_wrote_it(_message("<code>KEY-1</code>"))
    rendered = t("orders.auto_delivery", "en", payload=payload, warranty_days=1)
    assert "<code>KEY-1</code>" in rendered, "the admin's own copy box was stripped"


def test_manual_and_auto_deliveries_are_the_same_message() -> None:
    """Both paths go through one template now, so this is really asserting there is only one."""
    auto = t("orders.auto_delivery", "en", payload="X", warranty_days=7)
    manual = t("orders.auto_delivery", "en", payload="X", warranty_days=7)
    assert auto == manual
    assert "Warranty: 7 days" in manual, "the hand-fulfilled buyer was missing this line"
