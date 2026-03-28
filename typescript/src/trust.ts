import { ALPAuthError, ALPValidationError } from "./errors.js";
import { PeerConfig } from "./types.js";

export interface TrustedPeer {
  agent_id: string;
  public_keys: Record<string, string>;
  allowed_task_types?: string[];
  callback_domain_allowlist?: string[];
  requests_per_minute?: number;
}

export class TrustStore {
  private readonly peers = new Map<string, TrustedPeer>();

  addPeer(peer: TrustedPeer): void {
    this.peers.set(peer.agent_id, peer);
  }

  static fromPeerConfigs(peers: PeerConfig[]): TrustStore {
    const store = new TrustStore();
    peers.forEach((peer) => {
      store.addPeer({
        agent_id: peer.agent_id,
        public_keys: peer.public_keys,
        allowed_task_types: peer.allowed_task_types ?? [],
        callback_domain_allowlist: peer.callback_domain_allowlist ?? []
      });
    });
    return store;
  }

  requirePeer(agentId: string): TrustedPeer {
    const peer = this.peers.get(agentId);
    if (!peer) {
      throw new ALPAuthError(`unknown issuer ${agentId}`);
    }
    return peer;
  }

  getPublicKey(agentId: string, keyId: string): string {
    const peer = this.requirePeer(agentId);
    const key = peer.public_keys[keyId];
    if (!key) {
      throw new ALPAuthError(`unknown key_id ${keyId} for issuer ${agentId}`);
    }
    return key;
  }

  validateTaskType(agentId: string, taskType: string): void {
    const peer = this.requirePeer(agentId);
    if (peer.allowed_task_types?.length && !peer.allowed_task_types.includes(taskType)) {
      throw new ALPValidationError(`task_type ${taskType} is not allowed for issuer ${agentId}`);
    }
  }

  callbackAllowlist(agentId: string): string[] {
    return this.requirePeer(agentId).callback_domain_allowlist ?? [];
  }
}

