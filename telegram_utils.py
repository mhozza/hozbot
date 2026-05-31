from html.parser import HTMLParser

__all__ = ["sanitize_telegram_html"]

# Tags that never carry attributes (always valid)
_TAG_NO_ATTRS: frozenset[str] = frozenset({
    "b", "strong", "i", "em", "u", "ins",
    "s", "strike", "del", "tg-spoiler", "pre",
})

# All allowed attributes per tag (union of optional & required attrs)
_TAG_ATTRS: dict[str, frozenset[str]] = {
    "code": frozenset({"class"}),
    "blockquote": frozenset({"expandable"}),
    "a": frozenset({"href"}),
    "tg-emoji": frozenset({"emoji-id"}),
    "tg-time": frozenset({"unix", "format"}),
    "span": frozenset({"class"}),
}

# Tags that require a *specific* attribute to be emitted (value -> attr name)
_TAG_REQUIRED_ATTR: dict[str, str] = {
    "a": "href",
    "tg-emoji": "emoji-id",
    "tg-time": "unix",
    "span": "class",
}

_ALL_ALLOWED: frozenset[str] = (
    _TAG_NO_ATTRS | frozenset(_TAG_ATTRS.keys())
)

# Attribute value restrictions: (tag, attr) -> frozenset of allowed values
_ATTR_VALUES: dict[tuple[str, str], frozenset[str]] = {
    ("span", "class"): frozenset({"tg-spoiler"}),
}


def _valid_attrs(tag: str, raw: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    """Filter *raw* attrs to those allowed for *tag*, with value restrictions."""
    allowed = _TAG_ATTRS.get(tag, frozenset())
    filtered: list[tuple[str, str | None]] = []
    for k, v in raw:
        if k not in allowed:
            continue
        restricted = _ATTR_VALUES.get((tag, k))
        if restricted is not None and v not in restricted:
            continue
        filtered.append((k, v))
    return filtered


class _TelegramSanitizer(HTMLParser):
    """HTMLParser that rebuilds HTML keeping only Telegram-allowed tags.

    Does *not* track nesting depth.  If an unsupported tag wraps supported
    content (e.g. ``<p>text <b>bold</b></p>``), the inner supported tags are
    still emitted.  This trades perfect structural hygiene for practical
    robustness.
    """

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._suppressed_starts: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _ALL_ALLOWED:
            return

        if tag in _TAG_NO_ATTRS:
            self._parts.append(f"<{tag}>")
            return

        filtered = _valid_attrs(tag, attrs)
        required = _TAG_REQUIRED_ATTR.get(tag)
        if required is not None and not any(k == required for k, _ in filtered):
            self._suppressed_starts.add(tag)
            return

        attr_str = "".join(
            f' {k}="{v}"' if v is not None else f" {k}"
            for k, v in filtered
        )
        self._parts.append(f"<{tag}{attr_str}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._suppressed_starts:
            self._suppressed_starts.discard(tag)
            return
        if tag in _ALL_ALLOWED:
            self._parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self._parts.append(data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def handle_entityref(self, name: str) -> None:
        self._parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._parts.append(f"&#{name};")

    def get_result(self) -> str:
        self.close()
        return "".join(self._parts)


def sanitize_telegram_html(text: str) -> str:
    """Strip unsupported HTML tags, keeping only Telegram-compatible ones."""
    parser = _TelegramSanitizer()
    parser.feed(text)
    return parser.get_result()
