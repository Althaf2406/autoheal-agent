import re
from dataclasses import dataclass

@dataclass
class RedactResult:
    clean_text: str
    entity_count: int

# Using standard regexes for secrets and PII
PATTERNS = {
    "AWS_KEY": r"\bAKIA[0-9A-Z]{16}\b",
    "GITHUB_TOKEN": r"\bgh[pousr]_[a-zA-Z0-9]{36}\b",
    "JWT": r"\bey[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\b",
    "CREDENTIALS_URL": r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]+)://(?P<user>[^:]+):(?P<password>[^@]+)@",
    "EMAIL": r"\b[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+\b",
    "CREDIT_CARD": r"\b(?:\d{4}[ -]?){3}\d{4}\b"
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
