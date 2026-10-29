"""URL slugs."""

import re
import unicodedata


def slugify(text: str) -> str:
    """Lowercase ASCII words joined by single dashes, with no dash at either end."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "-", ascii_text.lower())
