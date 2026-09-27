# AGENTS.md

## Structure
- Solution (ours): `bot.py` — FastAPI app (`bot:app`) + full engine. Entry points: `compose()` / `compose_message()` (offline), `_conversation_reply()` (reply policy), endpoints `/v1/healthz|metadata|context|tick|reply|teardown`. `conversation_handlers.py` — offline wrapper (`respond()`) around `_conversation_reply`; no HTTP. `generate_submission.py` — expanded `dataset/` (`test_pairs.json`, `categories/|merchants/|customers/|triggers/`) → `submission.jsonl`.
- Challenger (mixed into this dir, do not edit): `challenge-brief.md`, `challenge-testing-brief.md`, `engagement-design.md`, `engagement-research.md`, `examples/`, `dataset/` (seeds only: `categories/*.json`, `customers/merchants/triggers_seed.json`, `generate_dataset.py`), `judge_simulator.py`.
- `original_Challenge_Tree.md` — reference snapshot of the challenger package shape; consult when asked challenge-specific questions.

## Commands
- Setup: `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt` (deps: only `fastapi`, `uvicorn`, `pydantic`)
- Run: `uvicorn bot:app --host 0.0.0.0 --port 8080`; check `curl http://127.0.0.1:8080/v1/healthz`
- Expand dataset (seeds → test bed): `python dataset/generate_dataset.py --seed-dir dataset --out dataset_expanded` (no `test_pairs.json` committed — you must generate it)
- Offline submission: `python generate_submission.py --dataset dataset_expanded --out submission.jsonl` (`submission.jsonl` is currently NOT committed — regenerate after changing composition)
- Live judge: edit `BOT_URL`/`LLM_PROVIDER`/`LLM_API_KEY` in `judge_simulator.py:25-45`, then `BOT_URL=http://127.0.0.1:8080 python judge_simulator.py` (requires own LLM key; never commit it)
- No test/lint/typecheck suite — verify with `python -m py_compile bot.py` plus the smoke sequence (`healthz → context → stale-context-409 → tick → reply → teardown`).

## Constraints (judge-enforced, do not break)
- Deterministic, no external network calls — never add LLM/API calls or API keys.
- `POST /v1/context`: higher version atomically replaces; stale/lower version must return HTTP 409 `{"accepted": false, "reason": "stale_version"}`.
- Message pipeline in `compose()`: `_route()` → `_sanitize()` strips `http(s)://` URLs → dedupe via `prior_bodies`/`sent` set + `_make_nonrepeat()` → `_shorten()` caps at 1000 chars preserving trailing CTA. Keep all three.
- Routing: `send_as` is `merchant_on_behalf` iff `trigger.scope == "customer"` or customer present, else `vera` (`bot.py:180`). Customer-facing CTAs are `binary_yes_no`; merchant `milestone_reached` uses `cta: none`.
- `/v1/tick`: max 20 actions, skips already-fired `suppression_key`s and unknown merchant/category; `/v1/teardown` wipes `STORE/CHATS/FIRED/ENDED`.
- `/v1/reply` must tolerate unknown `conversation_id` by creating recoverable state, not 404/500; `ended` chats stay `end`.
- `/v1/metadata` reads `TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL` env vars.
