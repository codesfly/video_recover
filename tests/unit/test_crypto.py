import stat

from video_recover.crypto import CookieVault


def test_cookie_is_encrypted_at_rest(tmp_path):
    vault = CookieVault(tmp_path / "app.key")
    token = vault.encrypt("sessionid=top-secret")

    assert b"top-secret" not in token
    assert vault.decrypt(token) == "sessionid=top-secret"


def test_generated_key_is_user_read_write_only(tmp_path):
    key_path = tmp_path / "secrets" / "app.key"
    CookieVault(key_path)

    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_existing_key_decrypts_values_after_restart(tmp_path):
    key_path = tmp_path / "app.key"
    first = CookieVault(key_path)
    token = first.encrypt("sessionid=persistent")

    second = CookieVault(key_path)
    assert second.decrypt(token) == "sessionid=persistent"

