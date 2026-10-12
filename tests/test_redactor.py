import pytest
from backend.redactor import redact

def test_redact_aws_key():
    log = "Error accessing S3. AWS Key: AKIAIOSFODNN7EXAMPLE is invalid."
    result = redact(log)
    assert "AKIAIOSFODNN7EXAMPLE" not in result.clean_text
    assert "<AWS_KEY_REDACTED>" in result.clean_text
    assert result.entity_count == 1

def test_redact_github_token():
    log = "fatal: could not read Username for 'https://github.com'. Token ghp_123456789012345678901234567890123456 failed."
    result = redact(log)
    assert "ghp_123456789012345678901234567890123456" not in result.clean_text
    assert "<GITHUB_TOKEN_REDACTED>" in result.clean_text
    assert result.entity_count == 1

def test_redact_email():
    log = "Contact admin@enterprise.com or support@company.co.id for help."
    result = redact(log)
    assert "admin@enterprise.com" not in result.clean_text
    assert "support@company.co.id" not in result.clean_text
    assert "<EMAIL_REDACTED>" in result.clean_text
    assert result.entity_count == 2

def test_redact_db_credentials():
    log = "DB connection failed for postgresql://admin:super_secret_p4ss@db.prod.com:5432/main_db"
    result = redact(log)
    assert "admin:super_secret_p4ss" not in result.clean_text
    assert "postgresql://<REDACTED_USER>:<REDACTED_PASSWORD>@db.prod.com:5432/main_db" in result.clean_text
    assert result.entity_count == 1

def test_redact_jwt():
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    log = f"Auth failed with token {jwt}!"
    result = redact(log)
    assert jwt not in result.clean_text
    assert "<JWT_REDACTED>" in result.clean_text
    assert result.entity_count == 1

def test_redact_credit_card():
    log = "Payment failed for card 1234-5678-9012-3456 due to insufficient funds."
    result = redact(log)
    assert "1234-5678-9012-3456" not in result.clean_text
    assert "<CREDIT_CARD_REDACTED>" in result.clean_text
    assert result.entity_count == 1

def test_redact_multiple():
    log = "User test@test.com failed. AWS: AKIAIOSFODNN7EXAMPLE, Token: ghp_123456789012345678901234567890123456. DB: mysql://user:pass@localhost"
    result = redact(log)
    assert "test@test.com" not in result.clean_text
    assert "AKIAIOSFODNN7EXAMPLE" not in result.clean_text
    assert "ghp_" not in result.clean_text
    assert "mysql://<REDACTED_USER>:<REDACTED_PASSWORD>@localhost" in result.clean_text
    assert result.entity_count == 4
