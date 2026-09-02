"""Language names for the expected-languages picker.

The providers want ISO 639-1 codes; people do not think in ISO 639-1. This maps between
them so the UI can offer "English", "Português" and so on.

Not exhaustive on purpose -- it covers the languages the transcription models actually
handle well, and any code already in a config file is preserved and shown even if it is
not listed here, so a hand-edited setting is never silently dropped.
"""

from __future__ import annotations

# code -> (English name, endonym). The endonym is shown alongside because someone looking
# for their own language scans for it, not for the English word.
LANGUAGES: dict[str, tuple[str, str]] = {
    "en": ("English", "English"),
    "es": ("Spanish", "Español"),
    "fr": ("French", "Français"),
    "de": ("German", "Deutsch"),
    "it": ("Italian", "Italiano"),
    "pt": ("Portuguese", "Português"),
    "nl": ("Dutch", "Nederlands"),
    "pl": ("Polish", "Polski"),
    "ru": ("Russian", "Русский"),
    "uk": ("Ukrainian", "Українська"),
    "tr": ("Turkish", "Türkçe"),
    "sv": ("Swedish", "Svenska"),
    "no": ("Norwegian", "Norsk"),
    "da": ("Danish", "Dansk"),
    "fi": ("Finnish", "Suomi"),
    "cs": ("Czech", "Čeština"),
    "el": ("Greek", "Ελληνικά"),
    "ro": ("Romanian", "Română"),
    "hu": ("Hungarian", "Magyar"),
    "he": ("Hebrew", "עברית"),
    "ar": ("Arabic", "العربية"),
    "fa": ("Persian", "فارسی"),
    "hi": ("Hindi", "हिन्दी"),
    "bn": ("Bengali", "বাংলা"),
    "ur": ("Urdu", "اردو"),
    "ta": ("Tamil", "தமிழ்"),
    "th": ("Thai", "ไทย"),
    "vi": ("Vietnamese", "Tiếng Việt"),
    "id": ("Indonesian", "Bahasa Indonesia"),
    "ms": ("Malay", "Bahasa Melayu"),
    "zh": ("Chinese", "中文"),
    "ja": ("Japanese", "日本語"),
    "ko": ("Korean", "한국어"),
    "af": ("Afrikaans", "Afrikaans"),
    "sw": ("Swahili", "Kiswahili"),
}


def label_for(code: str) -> str:
    """A display label for a code, including ones not in the table."""
    entry = LANGUAGES.get(code)
    if entry is None:
        return f"{code}  (unrecognised code)"
    english, endonym = entry
    return english if english == endonym else f"{english}  ({endonym})"


def sorted_codes(include: list[str] | None = None) -> list[str]:
    """Known languages by English name, with any unrecognised codes kept at the end.

    Preserving unknown codes matters: a config edited by hand, or written by a future
    version that knows more languages, must not lose entries just because this table is
    out of date.
    """
    known = sorted(LANGUAGES, key=lambda c: LANGUAGES[c][0])
    extra = [c for c in (include or []) if c not in LANGUAGES]
    return known + sorted(extra)
