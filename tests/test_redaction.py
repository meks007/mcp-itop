"""Tests for safe secret logging."""

from client import _redact_form_data, _redact_secret


def test_redact_secret_retains_final_six_characters():
    assert _redact_secret("super-secret-token-123456") == "***REDACTED***123456"


def test_redact_short_secret_fully():
    assert _redact_secret("123456") == "***REDACTED***"


def test_redact_form_data_masks_auth_secrets():
    data = {
        "auth_token": "token-abcdef123456",
        "auth_pwd": "password-789012",
        "version": "1.3",
    }

    redacted = _redact_form_data(data)

    assert redacted == {
        "auth_token": "***REDACTED***123456",
        "auth_pwd": "***REDACTED***789012",
        "version": "1.3",
    }
    assert data["auth_token"] == "token-abcdef123456"
    assert data["auth_pwd"] == "password-789012"
