from __future__ import annotations

from aiogram.types import LinkPreviewOptions

from app.core.config import get_settings
from app.locales.i18n import t

# The home screen carries links, and Telegram would otherwise staple a fat channel/group preview
# card under every render of it. Pass this at every site that sends or edits `home_body`.
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _link_label(url: str) -> str:
    """The link as the buyer reads it: the address itself, minus the scheme, so they can see exactly
    where it goes before tapping. Works for public usernames and private invite links alike."""
    return url.split("://", 1)[-1].rstrip("/")


def home_body(locale: str, name: str | None) -> str:
    """The one and only text of the main menu screen.

    It lives here rather than in `/start` because the user reaches this screen from at least six
    places — /start, Home, Back, a language change, and cancelling any of the forms. When each of
    those built its own text, only /start ever grew the community block and every other route
    quietly served a shorter welcome, which read as the bot losing the invite.

    The channel and the group are independent: each line is dropped when its URL is blank, and the
    whole "official links" block goes when both are, so a deployment never advertises a dead link.
    """
    settings = get_settings()
    body = t("welcome.subtitle", locale, name=name or "there")

    links: list[str] = []
    channel_url = settings.community_channel_url.strip()
    if channel_url:
        links.append(t("welcome.channel", locale, url=channel_url, label=_link_label(channel_url)))
    group_url = settings.community_group_url.strip()
    if group_url:
        links.append(t("welcome.group", locale, url=group_url, label=_link_label(group_url)))
    if links:
        body += t("welcome.links_title", locale) + "".join(links) + t("welcome.links_note", locale)

    return body + t("welcome.cta", locale)
