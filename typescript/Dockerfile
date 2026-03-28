FROM node:20-slim

WORKDIR /app
COPY schemas ./schemas
COPY sql ./sql
COPY typescript ./typescript

WORKDIR /app/typescript
RUN npm ci && npm run build

CMD ["node", "dist/examples/scoring-agent.js"]
