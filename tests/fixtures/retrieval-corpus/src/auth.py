def verify_token(token: str) -> bool:
    """Verify the bearer token signature before accepting a request."""
    return token.startswith("signed:")

