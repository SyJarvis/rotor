from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta
from typing import Optional
from rotor.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_token(plain_token: str, hashed_token: str) -> bool:
    """Verify a token against a hashed token."""
    return pwd_context.verify(plain_token, hashed_token)


def get_token_hash(token: str) -> str:
    """Hash a token."""
    return pwd_context.hash(token)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(days=1)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> Optional[dict]:
    """Decode a JWT access token."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except JWTError:
        return None


def validate_api_key_format(key: str) -> bool:
    """Validate API key format."""
    if not key:
        return False
    if not key.startswith(settings.API_KEY_PREFIX):
        return False
    return len(key) >= len(settings.API_KEY_PREFIX) + 10


def mask_api_key(key: str, visible_chars: int = 8) -> str:
    """Mask an API key for logging/display."""
    if len(key) <= visible_chars + 4:
        return key[:4] + "..."
    return key[:visible_chars] + "..." + key[-4:]
