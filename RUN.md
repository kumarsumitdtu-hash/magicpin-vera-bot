# Run / validation notes

The challenge package supplies `dataset/generate_dataset.py` and `judge_simulator.py`.
Keep those challenge files beside this solution when testing.

1. Generate the expanded dataset:
   `python dataset/generate_dataset.py --seed-dir dataset --out dataset_expanded`
   (If using the challenge's normal layout, point `--seed-dir` at the seed directory.)

2. Start:
   `uvicorn bot:app --host 0.0.0.0 --port 8080`

3. Check:
   `curl http://127.0.0.1:8080/v1/healthz`

4. Generate submission:
   `python generate_submission.py --dataset dataset_expanded --out submission.jsonl`

5. For the official simulator, set `BOT_URL=http://127.0.0.1:8080` and run
   the challenge's `judge_simulator.py`.

The stale-context path deliberately returns HTTP 409 with `accepted:false`, while
higher versions atomically replace older context entries.
