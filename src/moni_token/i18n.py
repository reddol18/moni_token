"""Message catalogs and the choice of language (issue #12).

One JSON file per language in `locales/`. Keys carry `{placeholders}`; Python-side strings may use format specs
(`{share:.0%}`), page strings (keys starting with "p.") only plain `{name}`, filled in by the page's `T()`.
Nothing is translated at runtime: a missing key falls back to English, then to the key itself.

Language, first match wins: explicit (`--lang`, `?lang=`) > `lang` in ~/.moni_token/config.toml > OS locale > English.
"""
import json
import os
import sys
from functools import cache
from pathlib import Path

DIR = Path(__file__).parent / "locales"
DEFAULT = "en"
NATIVE = {"en": "English", "ko": "한국어"}   # the language toggle shows the other language by its own name


@cache
def available() -> tuple[str, ...]:
    return tuple(sorted(p.stem for p in DIR.glob("*.json")))


@cache
def catalog(lang: str) -> dict[str, str]:
    return json.loads((DIR / f"{lang}.json").read_text(encoding="utf-8"))


def t(key: str, lang: str = DEFAULT, /, **kw) -> str:
    cat = catalog(lang if lang in available() else DEFAULT)
    s = cat.get(key)
    if s is None:
        s = catalog(DEFAULT).get(key, key)
    return s.format(**kw) if kw else s


def unformat(template: str, text: str) -> dict | None:
    """The placeholder values that make `template` produce `text`, or None if it doesn't match.
    Used once per stored sentence written before issue #12 (Korean only, no keys): match it against the Korean
    template, recover the numbers, re-render in any language. Format specs handled: ,  .Nf  .N%."""
    import re
    import string   # imported here: the status line imports this module and needs neither
    rx, specs = [], {}
    for lit, field, spec, _ in string.Formatter().parse(template):
        rx.append(re.escape(lit))
        if field is None:
            continue
        if field in specs:
            rx.append(f"(?P={field})")
            continue
        specs[field] = spec or ""
        if spec:
            rx.append(f"(?P<{field}>-?[\\d,]+(?:\\.\\d+)?)" + ("%" if spec.endswith("%") else ""))
        else:
            rx.append(f"(?P<{field}>.+?)")
    m = re.fullmatch("".join(rx), text, re.S)
    if not m:
        return None
    out: dict = {}
    for field, spec in specs.items():
        v = m.group(field)
        if spec:
            num = float(v.replace(",", ""))
            out[field] = num / 100 if spec.endswith("%") else int(num) if spec == "," else num
        else:
            out[field] = int(v) if v.isdigit() else v
    return out


def page_strings(lang: str) -> dict[str, str]:
    """What a page embeds for its JS: the "p." keys, English filling any gap."""
    out = {k: v for k, v in catalog(DEFAULT).items() if k.startswith("p.")}
    out.update({k: v for k, v in catalog(lang if lang in available() else DEFAULT).items() if k.startswith("p.")})
    return out


def normalize(code: str | None) -> str | None:
    """'ko_KR.UTF-8', 'ko-KR', 'Korean_Korea', 'en' -> a catalog we have, or None."""
    if not code or not isinstance(code, str):
        return None
    c = code.strip().replace("-", "_").split(".")[0].split("@")[0].lower()
    head = {"korean": "ko", "english": "en"}.get(c.split("_")[0], c.split("_")[0])
    return head if head in available() else None


def os_locale() -> str | None:
    """Kept cheap (the status line calls it): no ctypes, no locale module on Windows."""
    if sys.platform == "win32":   # the user's Windows locale; LANG under Git Bash may not follow it
        try:
            import _locale
            return _locale._getdefaultlocale()[0]
        except Exception:
            pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        import locale
        return locale.getlocale()[0]
    except Exception:
        return None


def config_lang(path: Path | None = None) -> str | None:
    """Top-level `lang = "en"` in ~/.moni_token/config.toml, or None."""
    from . import config
    p = path or (config.data_dir() / "config.toml")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    import tomllib   # only when there is a config file
    try:
        v = tomllib.loads(text).get("lang")
    except tomllib.TOMLDecodeError:
        return None
    return v if isinstance(v, str) else None


def resolve_lang(explicit: str | None = None, config_path: Path | None = None, os_code: str | None = None) -> str:
    for cand in (explicit, config_lang(config_path)):
        n = normalize(cand)
        if n:
            return n
    return normalize(os_code if os_code is not None else os_locale()) or DEFAULT


def warning_text(w: dict, lang: str) -> str:
    """The status line warning from the numbers `check` stored (spike_now.json), in the current language."""
    head = (f"+{w['rise_pp']:.1f}%p" if w.get("kind") == "pct" and w.get("rise_pp") is not None
            else t("status.cache_write", lang))
    label = t(f"cause.{w['code']}.label", lang) if w.get("code") else ""
    return "⚠ " + " · ".join(x for x in (head, label) if x)
