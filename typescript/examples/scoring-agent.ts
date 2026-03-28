import { ALPExecutionError } from "../src/errors.js";
import { ALPServer } from "../src/server.js";
import { FileTaskStore } from "../src/store.js";
import { TrustStore } from "../src/trust.js";
import { TaskEnvelope, TaskExecutor } from "../src/types.js";

function env(name: string, fallback?: string): string {
  const value = process.env[name] ?? fallback;
  if (!value) {
    throw new Error(`missing environment variable ${name}`);
  }
  return value;
}

const AGENT_ID = env("ALP_AGENT_ID", "scoring-agent-b");
const KEY_ID = env("ALP_KEY_ID", "agent-b-key");
const PRIVATE_KEY = env("ALP_PRIVATE_KEY", "Ic_J31AJyKqUwb58vqVKr5sHsyvlzh5gUq_oFcn151Y");
const TRUST_ISSUER = env("ALP_TRUST_ISSUER", "research-agent-a");
const TRUST_PUBLIC_KEY = env("ALP_TRUST_PUBLIC_KEY", "QiW6d7ewMiCwfJpTebEasOZ8XdDC5664y-FDbVCh1wQ");
const STORE_PATH = env("ALP_STORE_PATH", "./demo-data/agent-b-store.json");
const PORT = Number(env("PORT", "8080"));

class HeuristicScoringExecutor implements TaskExecutor {
  canHandle(task: TaskEnvelope): boolean {
    return task.task_type === "com.acme.score_ideas.v1";
  }

  async execute(task: TaskEnvelope): Promise<Record<string, unknown>> {
    const ideas = task.inputs.ideas;
    if (!Array.isArray(ideas)) {
      throw new ALPExecutionError("VALIDATION_ERROR", "inputs.ideas must be an array");
    }
    const scored = ideas.map((idea, index) => {
      const record = idea as Record<string, unknown>;
      const title = String(record.title ?? "");
      const problem = String(record.problem ?? "");
      const approach = String(record.approach ?? "");
      const combined = `${title} ${problem} ${approach}`.toLowerCase();

      const market = clamp(scoreForKeywords(combined, ["teams", "monitor", "budget", "audit", "ops", "compliance"], 4) + 4);
      const feasibility = clamp(scoreForKeywords(combined, ["monitor", "router", "audit", "score", "health"], 5) + 3);
      const novelty = clamp(scoreForKeywords(combined, ["mcp", "rag", "agent", "delegat", "drift"], 5) + 2);

      return {
        id: String(record.id ?? `idea-${index + 1}`),
        market_score: market,
        feasibility_score: feasibility,
        novelty_score: novelty,
        top_risks: buildRisks(combined),
        rationale: `Scored from problem clarity, operational urgency, and implementation directness for ${title}.`
      };
    });

    return { ideas: scored };
  }
}

function scoreForKeywords(text: string, keywords: string[], maxBonus: number): number {
  const matches = keywords.filter((keyword) => text.includes(keyword)).length;
  return Math.min(matches, maxBonus);
}

function clamp(value: number): number {
  return Math.max(0, Math.min(10, value));
}

function buildRisks(text: string): string[] {
  const risks = [];
  if (text.includes("compliance") || text.includes("audit")) {
    risks.push("Needs strong evidence quality and auditability.");
  }
  if (text.includes("rag") || text.includes("retrieval")) {
    risks.push("Evaluation quality depends on benchmark coverage.");
  }
  if (text.includes("agent") || text.includes("router")) {
    risks.push("Routing policies can become complex without tight guardrails.");
  }
  if (risks.length === 0) {
    risks.push("Requires clear differentiation against internal tooling.");
  }
  return risks.slice(0, 3);
}

const trustStore = new TrustStore();
trustStore.addPeer({
  agent_id: TRUST_ISSUER,
  public_keys: { "agent-a-key": TRUST_PUBLIC_KEY },
  allowed_task_types: ["com.acme.score_ideas.v1"],
  callback_domain_allowlist: ["agent-a", "localhost", "127.0.0.1"]
});

const server = new ALPServer({
  agentId: AGENT_ID,
  trustStore,
  store: new FileTaskStore(STORE_PATH),
  executor: new HeuristicScoringExecutor(),
  keyId: KEY_ID,
  privateKey: PRIVATE_KEY
});

const app = server.createApp();

app.listen({ host: "0.0.0.0", port: PORT }).then(() => {
  app.log.info({ agent: AGENT_ID, port: PORT }, "ALP scoring agent listening");
});

