"""Mask recognizable credentials at Readback's review storage and report boundaries."""

import json
import re


MASK = "<secret>"
# Match names, including environment/CLI prefixes, without treating ordinary prose
# about keys and tokens as a credential. A textual value requires an assignment.
_NAME = (r"(?:[a-z0-9]+[_-])*(?:pass(?:word|wd)|pwd|token|secret|"
         r"(?:api|private|secret|access)[_-]?key|(?:access|auth|refresh|session)[_-]?token|"
         r"client[_-]?secret|connection[_-]?string|database[_-]?url)")
_YAML_BLOCK = re.compile(
    r"^(?P<indent>[ \t]*)(?P<name>" + _NAME + r"[ \t]*:[ \t]*)[|>][0-9+\-]*[ \t]*\r?\n"
    r"(?:(?P=indent)[ \t]+[^\n]*(?:\n|\Z)|[ \t]*\n)+", re.I | re.M | re.ASCII)
_NAME_ONLY = re.compile(_NAME, re.I | re.ASCII)
_ASSIGN = re.compile(
    r"""(?<![\w-])(?P<name>(?:--)?["']?(?:authorization|""" + _NAME + r""")["']?[ \t]*[:=][ \t]*)"""
    # An escaped opening quote in a free-form report is generally an embedded
    # serialization. Mask through the end of that line when its boundary is ambiguous.
    r"""(?P<value>\\["'][^\n]*|"(?:[^"\\]|\\[\s\S])*(?:"|(?:\\)?\Z)|'(?:[^'\\]|\\[\s\S])*(?:'|(?:\\)?\Z)|"""
    r"""(?:bearer|basic)[ \t]+[^\s,;&"'\x60]+|(?:\\[\s\S]|[^\s,;&"'\x60\\])+)""",
    re.I | re.ASCII,
)
_AUTH = re.compile(r"\b(Bearer)([ \t]+)([A-Za-z0-9._~+/=-]{8,})", re.I | re.ASCII)
_URI = re.compile(r"\b([a-z][a-z0-9+.\-]{0,31}://)[^\s/@?#]+@", re.I | re.ASCII)
_PREFIX = re.compile(r"(?<![A-Za-z0-9])(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{10,}|"
                     r"(?:gh[pousr]_|github_pat_|glpat-|[sr]k_live_|xox[baprs]-)[A-Za-z0-9_-]{8,}|"
                     r"(?:AKIA|ASIA)[A-Z0-9]{16}\b|AIza[A-Za-z0-9_-]{35}\b)")
_PEM = re.compile(r"-----BEGIN (?P<kind>(?:[A-Z0-9]+ )*PRIVATE KEY)-----"
                  r"[\s\S]*?(?:-----END (?P=kind)-----|\Z)")


def _assignment(match):
    name, value = match.group("name", "value")
    if value[:1] in ('"', "'"):
        return name + value[0] + MASK + value[0]
    return name + MASK


def redact(text):
    # A JSON handoff can carry quoted credentials inside a field. Inspect the
    # decoded string before re-encoding so escape sequences do not expose it.
    if text.lstrip().startswith(("{", "[")):
        try:
            value = json.loads(text)
            masked = redact_value(value)
            if masked == value:
                return text
            return json.dumps(masked, ensure_ascii=True) + ("\n" if text.endswith("\n") else "")
        except ValueError:
            pass
        except RecursionError:
            return MASK
    text = _PEM.sub(MASK, text)
    text = _YAML_BLOCK.sub(lambda m: m["indent"] + m["name"] + MASK + "\n", text)
    text = _URI.sub(lambda match: match[1] + MASK + "@", text)
    text = _ASSIGN.sub(_assignment, text)
    text = _AUTH.sub(lambda match: match[1] + match[2] + MASK, text)
    return _PREFIX.sub(MASK, text)


# The retry reader sees only a byte prefix. A delimiter or minimum token length
# needed for recognition may lie just beyond that prefix.
_PARTIAL_URI = re.compile(r"\b([a-z][a-z0-9+.\-]{0,31}://)[^\s/@?#]+\Z", re.I | re.ASCII)
_PARTIAL_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:sk-|gh[pousr]_|github_pat_|glpat-|[sr]k_live_|"
                            r"xox[baprs]-|AKIA|ASIA|AIza)[A-Za-z0-9_-]*\Z")
_PARTIAL_BEARER = re.compile(r"\b(Bearer[ \t]+)[A-Za-z0-9._~+/=-]+\Z", re.I | re.ASCII)


def redact_prefix(text):
    text = _PARTIAL_URI.sub(lambda m: m[1] + MASK, text)
    text = _PARTIAL_TOKEN.sub(MASK, text)
    text = _PARTIAL_BEARER.sub(lambda m: m[1] + MASK, text)
    return redact(text)


def redact_bytes(data):
    return redact(data.decode("utf-8", errors="surrogateescape")).encode("utf-8", errors="surrogateescape")


def redact_value(value):
    """Keep JSON structure and types in ordinary fields, masking credential values by key."""
    if isinstance(value, dict):
        # Round authorization stores the owner's event id, a reference in the
        # audit trail. HTTP authorization fields can carry credentials.
        round_reference = value.keys() == {"attempt", "fails_before", "authorization"}
        result = {}
        for key, item in value.items():
            credential = _NAME_ONLY.fullmatch(key) or (
                key.lower() == "authorization" and isinstance(item, str) and not round_reference)
            result[redact(key)] = MASK if credential else redact_value(item)
        return result
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return redact(value) if isinstance(value, str) else value
