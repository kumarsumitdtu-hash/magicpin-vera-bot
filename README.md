# Vera Challenge — Independent Solution v2

## Architecture

This submission is a fresh implementation rather than a modification/copy of another
submission. It uses a deterministic **evidence-selection and trigger-routing** engine:

1. `/v1/context` stores category, merchant, customer and trigger contexts by key/version.
2. A trigger router selects a message strategy based on `trigger.kind`.
3. The strategy pulls concrete facts from the supplied trigger, merchant, category digest,
   active offers and (when applicable) customer context.
4. A post-composition pass removes URLs, limits length and prevents exact repeats.
5. `/v1/reply` keeps conversation state and applies explicit policies for opt-out,
   automated replies, delays, commitment/action intent and off-topic messages.
6. `/v1/teardown` clears all in-memory test state.

No merchant/customer payload is sent to a third-party service. This makes local runs
deterministic and avoids API-key/rate-limit failures during judging.

## Files

- `bot.py` — FastAPI server and composition/state engine.
- `conversation_handlers.py` — direct-call wrapper around the conversation policy.
- `generate_submission.py` — creates the required `submission.jsonl`.
- `requirements.txt` — runtime dependencies.
- `RUN.md` — setup and validation commands.

## Run

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Health check:

```bash
curl http://127.0.0.1:8080/v1/healthz
```

Generate the challenge dataset from the supplied challenge package, then:

```bash
python generate_submission.py --dataset dataset --out submission.jsonl
```

The implementation is deterministic for the same contexts and conversation state.
