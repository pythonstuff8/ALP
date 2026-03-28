FROM python:3.12-slim

WORKDIR /app
COPY schemas ./schemas
COPY python ./python

RUN pip install --no-cache-dir -e ./python

WORKDIR /app/python
CMD ["python", "examples/research_agent.py"]

