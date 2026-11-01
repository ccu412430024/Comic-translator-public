"""Conservative search-only cleanup of bookstore volume/promotional suffixes."""
import re
import unicodedata


def search_title(value):
    title=unicodedata.normalize("NFKC",value).strip()
    original=title
    def promotional(match):
        label=re.sub(r"\s+","",match.group(1))
        # Strong marketing prefixes only; retain meaningful editions such as
        # 新装版, 完全版 and 合本版, and arbitrary bracketed subtitles.
        known=re.match(r"^(?:期間限定(?:無料|試し読み|公開)|無料(?:試し読み|お試し|公開)|(?:増量)?試し読み|お試し|電子(?:書籍|版)?限定|電子特典|限時免費|期間限定免費|免費試閱)",label)
        return "" if known or label=="無料" else match.group(0)
    for _ in range(6):
        clean=re.sub(r"\s*[【\[(]([^【】\[\]()]{1,200})[】\])]\s*$",promotional,title).strip()
        clean=re.sub(r"\s*(?::\s*(?:第\s*)?\d{1,3}\s*(?:巻|卷|話)?|(?:第\s*)?\d{1,3}\s*[巻卷]|\(\d{1,3}\))\s*$","",clean).strip()
        if clean==title:break
        title=clean
    return title or original


class ResearchError(ValueError):
    """Only controlled messages from this class may be shown to the user."""
    def __init__(self,code,message):
        super().__init__(message)
        self.code=code
