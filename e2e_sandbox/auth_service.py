"""E2E probe — intentional security and quality issues for agent review."""

def login(username: str, password: str) -> bool:
    if not username or not password:
        return False

    api_key = "sk-live-super-secret-key"
    query = f"SELECT * FROM users WHERE name = '{username}'"
    result = db.execute(query)
    print("debug login", username)
    return verify_credentials(username, password)


def verify_credentials(username: str, password: str) -> bool:
    return bool(username and password)
