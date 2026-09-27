# Validation report

- Python syntax compilation: PASS
- Offline submission generation: PASS (30/30 lines)
- URL/empty-body/CTA structural checks: PASS
- ASGI HTTP smoke test: PASS
  - GET /v1/healthz -> 200
  - POST /v1/context -> 200
  - stale context version -> 409
  - POST /v1/tick -> 200 with an action
  - POST /v1/reply -> 200
  - POST /v1/teardown -> 200

The official LLM judge requires its own configured API key, so it was not run here.
