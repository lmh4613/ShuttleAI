import pytest
from cryptography.fernet import Fernet

from token_crypto import (
    TokenCryptoError,
    decrypt_token,
    encrypt_token,
    validate_encryption_key,
)


def test_fernet_key_validation_and_round_trip():
    key = Fernet.generate_key()
    assert validate_encryption_key(key) == key
    ciphertext = encrypt_token("temporary-test-token", key)
    assert isinstance(ciphertext, bytes)
    assert b"temporary-test-token" not in ciphertext
    assert decrypt_token(ciphertext, key) == "temporary-test-token"


def test_wrong_key_and_invalid_key_fail_without_plaintext():
    key = Fernet.generate_key()
    ciphertext = encrypt_token("temporary-test-token", key)
    with pytest.raises(TokenCryptoError, match="decryption failed"):
        decrypt_token(ciphertext, Fernet.generate_key())
    with pytest.raises(TokenCryptoError, match="invalid"):
        validate_encryption_key("not-a-fernet-key")
    with pytest.raises(TokenCryptoError, match="invalid"):
        validate_encryption_key("한글-key")


def test_empty_and_null_tokens_use_null_policy():
    key = Fernet.generate_key()
    assert encrypt_token("", key) is None
    assert encrypt_token(None, key) is None
    assert decrypt_token(None, key) is None
