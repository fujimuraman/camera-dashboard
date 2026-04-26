"""認証（Flask-Login）"""
import hashlib
import os
from datetime import datetime
from flask_login import UserMixin, LoginManager

from db import get_db


login_manager = LoginManager()
login_manager.login_view = "login"


class User(UserMixin):
    def __init__(self, id, username):
        self.id = id
        self.username = username


def _get_salt() -> bytes:
    """パスワードソルトを取得。
    優先順位: 環境変数 PASSWORD_SALT > DB settings.password_salt
    DBに無ければ初回自動生成して保存（配布先ごとに固有のソルトに）"""
    env_salt = os.getenv("PASSWORD_SALT")
    if env_salt:
        return env_salt.encode()
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='password_salt'"
        ).fetchone()
        if row and row["value"]:
            return row["value"].encode()
        # 初回: 自動生成して保存
        new_salt = "salt-" + os.urandom(16).hex()
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('password_salt', ?)",
            (new_salt,),
        )
        return new_salt.encode()


def _hash(password: str) -> str:
    # pbkdf2 で簡易ハッシュ（MVP 用途、Phase 2 で bcrypt に変更検討）
    salt = _get_salt()
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000).hex()


@login_manager.user_loader
def load_user(user_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row:
            return User(row["id"], row["username"])
    return None


def verify_password(username: str, password: str) -> User | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash FROM users WHERE username=?",
            (username,),
        ).fetchone()
        if row and row["password_hash"] == _hash(password):
            return User(row["id"], row["username"])
    return None


def create_user(username: str, password: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, created_at) VALUES(?, ?, ?)",
            (username, _hash(password), datetime.utcnow().isoformat()),
        )


def change_password(username: str, current_password: str, new_password: str) -> tuple[bool, str]:
    """パスワード変更。現パスワードが正しくないと変更不可。"""
    if not new_password or len(new_password) < 6:
        return False, "新しいパスワードは6文字以上にしてください"
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, password_hash FROM users WHERE username=?", (username,)
        ).fetchone()
        if not row:
            return False, "ユーザーが存在しません"
        if row["password_hash"] != _hash(current_password):
            return False, "現在のパスワードが一致しません"
        conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (_hash(new_password), row["id"]),
        )
    return True, "パスワードを変更しました"


def change_username(current_username: str, current_password: str, new_username: str) -> tuple[bool, str]:
    """ユーザー名変更。現パスワードが必要。"""
    if not new_username or len(new_username) < 3:
        return False, "ユーザー名は3文字以上にしてください"
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, password_hash FROM users WHERE username=?", (current_username,)
        ).fetchone()
        if not row or row["password_hash"] != _hash(current_password):
            return False, "現在のパスワードが一致しません"
        exists = conn.execute(
            "SELECT 1 FROM users WHERE username=?", (new_username,)
        ).fetchone()
        if exists:
            return False, "そのユーザー名は既に使われています"
        conn.execute("UPDATE users SET username=? WHERE id=?", (new_username, row["id"]))
    return True, "ログインIDを変更しました"


def has_any_user() -> bool:
    """初期セットアップ判定: DB に user が1人以上いるか"""
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
        return bool(row and row["c"] > 0)


def ensure_initial_user():
    """互換性のため残す。新規 setup フローでは何もしない（DBに user 0件なら /setup 経由で作成）"""
    return None
