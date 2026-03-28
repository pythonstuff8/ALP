from __future__ import annotations

import secrets
from pathlib import Path

from alp.crypto import generate_keypair


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env_example = (repo_root / ".env.example").read_text()

    agent_a_private, agent_a_public = generate_keypair()
    agent_b_private, agent_b_public = generate_keypair()
    postgres_password = secrets.token_urlsafe(18)

    env_text = (
        env_example.replace("CHANGE_ME_AGENT_A_PRIVATE_KEY", agent_a_private)
        .replace("CHANGE_ME_AGENT_A_PUBLIC_KEY", agent_a_public)
        .replace("CHANGE_ME_AGENT_B_PRIVATE_KEY", agent_b_private)
        .replace("CHANGE_ME_AGENT_B_PUBLIC_KEY", agent_b_public)
        .replace("CHANGE_ME_POSTGRES_PASSWORD", postgres_password)
    )

    target = repo_root / ".env"
    target.write_text(env_text)
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
