from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class GatewayContext:
    request_id: str
    conversation_id: str
    request_protocol: str
    user_id: Optional[str]
    token_id: Optional[int]
    client_ip: str
