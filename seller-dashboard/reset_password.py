"""パスワードリセット CLI ツール
パスワードを忘れた場合にサーバー上で直接実行する。

使い方（サーバーに SSH 等でログインした状態で）:
    python reset_password.py <username> <new_password>

セキュリティ: このスクリプトはサーバーにアクセスできる管理者のみ実行可能。
"""
import sys
from datetime import datetime
from auth import _hash
from db import get_db, init_db


def reset(username: str, new_password: str) -> None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE username=?", (username,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (_hash(new_password), row["id"]),
            )
            print(f"✔ {username} のパスワードをリセットしました")
        else:
            conn.execute(
                "INSERT INTO users(username, password_hash, created_at) VALUES(?, ?, ?)",
                (username, _hash(new_password), datetime.utcnow().isoformat()),
            )
            print(f"✔ 新規ユーザー {username} を作成しました")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python reset_password.py <username> <new_password>")
        sys.exit(1)
    reset(sys.argv[1], sys.argv[2])
