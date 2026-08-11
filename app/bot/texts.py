from __future__ import annotations

from aiogram.types import LinkPreviewOptions

from app.core.config import get_settings
from app.locales.i18n import t

# The home screen carries a link, and Telegram would otherwise staple a fat group-preview card
# under every render of it. Pass this at every site that sends or edits `home_body`.
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def home_body(locale: str, name: str | None) -> str:
    """The one and only text of the main menu screen.

    It lives here rather than in `/start` because the user reaches this screen from at least six
    places — /start, Home, Back, a language change, and cancelling any of the forms. When each of
    those built its own text, only /start ever grew the community block and every other route
    quietly served a shorter welcome, which read as the bot losing the group invite.

    The block is dropped entirely when no group is configured, so a deployment without one never
    advertises a dead link.
    """
    body = t("welcome.subtitle", locale, name=name or "there")
    group_url = get_settings().community_group_url.strip()
    if group_url:
        body += t("welcome.community", locale, group_url=group_url)
    return body
