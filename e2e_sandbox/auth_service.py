"""Baseline sandbox module — replaced on the E2E branch with intentional issues."""

def verify_credentials(username: str, password: str) -> bool:
    return bool(username and password)
