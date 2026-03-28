from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ALPAuthError, ALPValidationError
from .types import PeerConfig


@dataclass(slots=True)
class TrustedPeer:
    agent_id: str
    public_keys: dict[str, str]
    allowed_task_types: list[str] = field(default_factory=list)
    callback_domain_allowlist: list[str] = field(default_factory=list)
    requests_per_minute: int = 60


class TrustStore:
    def __init__(self) -> None:
        self._peers: dict[str, TrustedPeer] = {}

    def add_peer(self, peer: TrustedPeer) -> None:
        self._peers[peer.agent_id] = peer

    @classmethod
    def from_peer_configs(cls, peers: list[PeerConfig]) -> "TrustStore":
        store = cls()
        for peer in peers:
            store.add_peer(
                TrustedPeer(
                    agent_id=peer.agent_id,
                    public_keys=peer.public_keys,
                    allowed_task_types=peer.allowed_task_types,
                    callback_domain_allowlist=peer.callback_domain_allowlist,
                )
            )
        return store

    def require_peer(self, issuer: str) -> TrustedPeer:
        peer = self._peers.get(issuer)
        if not peer:
            raise ALPAuthError(f"unknown issuer {issuer}")
        return peer

    def get_public_key(self, issuer: str, key_id: str) -> str:
        peer = self.require_peer(issuer)
        try:
            return peer.public_keys[key_id]
        except KeyError as exc:
            raise ALPAuthError(f"unknown key_id {key_id} for issuer {issuer}") from exc

    def validate_task_type(self, issuer: str, task_type: str) -> None:
        peer = self.require_peer(issuer)
        if peer.allowed_task_types and task_type not in peer.allowed_task_types:
            raise ALPValidationError(f"task_type {task_type} is not allowed for issuer {issuer}")

    def callback_allowlist(self, issuer: str) -> list[str]:
        return list(self.require_peer(issuer).callback_domain_allowlist)

