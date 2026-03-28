import { ALPExecutionError } from "../src/errors.js";
import { ALPServer } from "../src/server.js";
import { FileTaskStore, PostgresTaskStore, TaskStore } from "../src/store.js";
import { TrustStore } from "../src/trust.js";
import { TaskEnvelope, TaskExecutor } from "../src/types.js";

function env(name: string, fallback?: string): string {
  const value = process.env[name] ?? fallback;
  if (!value) {
    throw new Error(`missing environment variable ${name}`);
  }
  return value;
}

function envOptional(name: string): string | undefined {
  return process.env[name] ?? undefined;
}

const ALP_ENV = env("ALP_ENV", "development");
const EXECUTOR_MODE = env("ALP_EXECUTOR_MODE", "mock");
const AGENT_ID = env("ALP_AGENT_ID", "scoring-agent-b");
const KEY_ID = env("ALP_KEY_ID", "agent-b-key");
const PRIVATE_KEY = env("ALP_PRIVATE_KEY", "CHANGE_ME_AGENT_B_PRIVATE_KEY");
const TRUST_ISSUER = env("ALP_TRUST_ISSUER", "research-agent-a");
const TRUST_PUBLIC_KEY = env("ALP_TRUST_PUBLIC_KEY", "CHANGE_ME_AGENT_A_PUBLIC_KEY");
const PORT = Number(env("PORT", "8080"));
const STORE_PATH = env("ALP_STORE_PATH", "./demo-data/agent-b-store.json");
const DATABASE_URL = envOptional("ALP_DATABASE_URL");
const PUBLIC_BASE_URL = envOptional("ALP_PUBLIC_BASE_URL");
const ANTHROPIC_API_KEY = envOptional("ANTHROPIC_API_KEY");
const ANTHROPIC_MODEL_ID = envOptional("ANTHROPIC_MODEL_ID");

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

class AnthropicScoringExecutor implements TaskExecutor {
  constructor(
    private readonly apiKey: string,
    private readonly modelId: string
  ) {}

  canHandle(task: TaskEnvelope): boolean {
    return task.task_type === "com.acme.score_ideas.v1";
  }

  async execute(task: TaskEnvelope): Promise<Record<string, unknown>> {
    const ideas = task.inputs.ideas;
    if (!Array.isArray(ideas)) {
      throw new ALPExecutionError("VALIDATION_ERROR", "inputs.ideas must be an array");
    }

    const response = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-api-key": this.apiKey,
        "anthropic-version": "2023-06-01"
      },
      body: JSON.stringify({
        model: this.modelId,
        max_tokens: 1500,
        system:
          "Score startup or product ideas. Always return the final answer by calling the provided tool exactly once with a complete payload.",
        messages: [
          {
            role: "user",
            content: [
              {
                type: "text",
                text:
                  `Objective: ${task.objective}\n\n` +
                  `Constraints:\n${JSON.stringify(task.constraints, null, 2)}\n\n` +
                  `Ideas:\n${JSON.stringify(ideas, null, 2)}\n\n` +
                  "Score each idea on market, feasibility, novelty, and top risks."
              }
            ]
          }
        ],
        tools: [
          {
            name: "submit_scorecard",
            description: "Return the final scorecard payload for all ideas.",
            input_schema: task.expected_output_schema.schema
          }
        ],
        tool_choice: {
          type: "tool",
          name: "submit_scorecard"
        }
      })
    });

    if (!response.ok) {
      throw new ALPExecutionError("EXECUTION_ERROR", `Anthropic request failed: ${response.status} ${await response.text()}`);
    }

    const body = (await response.json()) as {
      content?: Array<{ type?: string; name?: string; input?: Record<string, unknown> }>;
    };
    const toolBlock = body.content?.find((block) => block.type === "tool_use" && block.name === "submit_scorecard");
    if (!toolBlock?.input) {
      throw new ALPExecutionError("EXECUTION_ERROR", "Anthropic did not return the required tool payload");
    }
    return toolBlock.input;
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

function validateRuntimeConfig(): void {
  if (ALP_ENV !== "development") {
    if (PRIVATE_KEY.startsWith("CHANGE_ME")) {
      throw new Error("ALP_PRIVATE_KEY must be configured outside development");
    }
    if (TRUST_PUBLIC_KEY.startsWith("CHANGE_ME")) {
      throw new Error("ALP_TRUST_PUBLIC_KEY must be configured outside development");
    }
  }
  if (EXECUTOR_MODE === "provider" && (!ANTHROPIC_API_KEY || !ANTHROPIC_MODEL_ID)) {
    throw new Error("provider mode requires ANTHROPIC_API_KEY and ANTHROPIC_MODEL_ID");
  }
}

function buildStore(): TaskStore {
  if (DATABASE_URL) {
    return new PostgresTaskStore(DATABASE_URL);
  }
  if (ALP_ENV === "development") {
    return new FileTaskStore(STORE_PATH);
  }
  throw new Error("ALP_DATABASE_URL is required outside development");
}

function buildExecutor(): TaskExecutor {
  if (EXECUTOR_MODE === "provider") {
    return new AnthropicScoringExecutor(ANTHROPIC_API_KEY!, ANTHROPIC_MODEL_ID!);
  }
  return new HeuristicScoringExecutor();
}

validateRuntimeConfig();

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
  store: buildStore(),
  executor: buildExecutor(),
  keyId: KEY_ID,
  privateKey: PRIVATE_KEY,
  publicBaseUrl: PUBLIC_BASE_URL
});

const app = server.createApp();

app.listen({ host: "0.0.0.0", port: PORT }).then(() => {
  app.log.info({ agent: AGENT_ID, mode: EXECUTOR_MODE, port: PORT }, "ALP scoring agent listening");
});
