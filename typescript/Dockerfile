FROM node:20-slim

WORKDIR /app
COPY schemas ./schemas
COPY typescript ./typescript

WORKDIR /app/typescript
RUN npm install

CMD ["npm", "run", "start:agent-b"]

