import re
from dataclasses import dataclass

@dataclass
class RedactResult:
    clean_text: str
    entity_count: int

    @property
    def text(self) -> str:
        return self.clean_text


RedactedLog = RedactResult

# Using standard regexes for secrets and PII
PATTERNS = {
    "PRIVATE_KEY": r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    # KEY=value / KEY: "value" where the name says it is a secret (password, token, api_key, ...)
    "SECRET_ASSIGNMENT": (r"(?i)\b(?P<name>[A-Z0-9_]*(?:passw(?:or)?d|pwd|secret|token|api_?key|access_?key)"
                          r"[A-Z0-9_]*)(?P<sep>\s*[=:]\s*[\"']?)[^\s\"']+"),
    "AWS_KEY": r"\bAKIA[0-9A-Z]{16}\b",
    "GITHUB_TOKEN": r"\b(?:gh[pousr]_[a-zA-Z0-9]{36}|github_pat_[A-Za-z0-9_]{22,})\b",
    "SLACK_TOKEN": r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b",
    "BEARER_TOKEN": r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*",
    "JWT": r"\bey[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\b",
    "CREDENTIALS_URL": r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]+)://(?P<user>[^:]+):(?P<password>[^@]+)@",
    "EMAIL": r"\b[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+\b",
    "CREDIT_CARD": r"\b(?:\d{4}[ -]?){3}\d{4}\b",
    # Indonesian mobile numbers: +62 812-3456-7890, 081234567890
    "PHONE": r"(?:\+62[ -]?|\b0)8\d{2}[ -]?\d{3,4}[ -]?\d{3,4}\b",
}

def redact(text: str) -> RedactResult:
    if not text:
        return RedactResult(clean_text=text if text is not None else "", entity_count=0)

    clean_text = text
    total_count = 0

    for entity_type, pattern in PATTERNS.items():
        if entity_type == "CREDENTIALS_URL":
            # Just count them
            matches = list(re.finditer(pattern, clean_text))
            total_count += len(matches)
            # Replace user and password but keep scheme and @
            clean_text = re.sub(pattern, r"\g<scheme>://<REDACTED_USER>:<REDACTED_PASSWORD>@", clean_text)
        elif entity_type == "SECRET_ASSIGNMENT":
            # Keep the variable name so the log still says what was wrong
            total_count += len(re.findall(pattern, clean_text))
            clean_text = re.sub(pattern, r"\g<name>\g<sep><SECRET_REDACTED>", clean_text)
        elif entity_type == "CREDIT_CARD":
             cc_pattern = r"\b(?:\d{4}[ -]?){3}\d{4}\b"
             matches = re.findall(cc_pattern, clean_text)
             total_count += len(matches)
             clean_text = re.sub(cc_pattern, "<CREDIT_CARD_REDACTED>", clean_text)
        else:
            matches = re.findall(pattern, clean_text)
            total_count += len(matches)
            clean_text = re.sub(pattern, f"<{entity_type}_REDACTED>", clean_text)

    return RedactResult(clean_text=clean_text, entity_count=total_count)
