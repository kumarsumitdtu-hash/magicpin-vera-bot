"""Create submission.jsonl from the challenge's expanded dataset."""

import argparse
import json
from pathlib import Path
from bot import compose_message

def read_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="dataset")
    parser.add_argument("--out", default="submission.jsonl")
    args = parser.parse_args()

    root = Path(args.dataset)
    pairs = read_json(root / "test_pairs.json")["pairs"]
    categories = {p.stem: read_json(p) for p in (root / "categories").glob("*.json")}
    merchants = {p.stem: read_json(p) for p in (root / "merchants").glob("*.json")}
    customers = {p.stem: read_json(p) for p in (root / "customers").glob("*.json")}
    triggers = {p.stem: read_json(p) for p in (root / "triggers").glob("*.json")}

    with open(args.out, "w", encoding="utf-8") as out:
        for pair in pairs:
            trigger = triggers[pair["trigger_id"]]
            merchant = merchants[pair["merchant_id"]]
            category = categories[merchant["category_slug"]]
            customer = customers.get(pair["customer_id"]) if pair.get("customer_id") else None
            result = compose_message(category, merchant, trigger, customer)
            row = {
                "test_id": pair["test_id"],
                "body": result["body"],
                "cta": result["cta"],
                "send_as": result["send_as"],
                "suppression_key": result["suppression_key"],
                "rationale": result["rationale"],
            }
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(pair["test_id"], result["body"][:100])

if __name__ == "__main__":
    main()
