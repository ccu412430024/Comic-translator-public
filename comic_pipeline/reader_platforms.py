"""Validated platform references, independent of image and translation engines."""
import re

BOOKWALKER = r"bookwalker:[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}"
PIXIV_EPISODE = r"pixiv:episode:[1-9][0-9]{0,11}"
PIXIV_WORK = r"pixiv:work:[1-9][0-9]{0,11}"
READER_REF = re.compile(rf"(?:{BOOKWALKER}|{PIXIV_EPISODE})")
CATALOGUE_REF = re.compile(rf"(?:{BOOKWALKER}|{PIXIV_WORK})")
PLATFORMS = ("bookwalker", "pixiv")


def platform_id(ref):
    """Classify validated references, including the legacy BookWalker title fallback."""
    if not isinstance(ref, str):
        return ""
    if re.fullmatch(BOOKWALKER, ref) or ref.startswith("title:") and len(ref) > 6:
        return "bookwalker"
    if re.fullmatch(rf"(?:{PIXIV_EPISODE}|{PIXIV_WORK})", ref):
        return "pixiv"
    return ""


def reader_ref(value):
    return isinstance(value, str) and READER_REF.fullmatch(value) is not None


def catalogue_ref(value):
    return isinstance(value, str) and (CATALOGUE_REF.fullmatch(value) is not None or value.startswith("title:") and len(value)>6)
