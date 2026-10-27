"""Collect pre-result public prices and later outcomes; no wager execution."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen

from autosport.prospective_shadow_observer import ObserverPlan, observe_cycle
from autosport.prospective_forecast_learning import forecast_learning_cycle


SOURCES = {"predictions": "https://tipsaudit.com/api/v1/predictions",
           "results": "https://tipsaudit.com/track-record.csv"}


def fetch(url):
    request = Request(url, headers={"User-Agent": "Autosport-paper-research/0.1 (read-only; attribution tipsaudit.com)"})
    with urlopen(request, timeout=30) as response:
        raw = response.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError("public source exceeds bounded response size")
    return raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=49)
    parser.add_argument("--interval-seconds", type=int, default=300)
    args = parser.parse_args()
    if not 1 <= args.cycles <= 49 or not 60 <= args.interval_seconds <= 300:
        parser.error("cycles must be 1..49 and interval 60..300 seconds")
    plan = ObserverPlan(**json.loads(args.config.read_text("utf-8")))
    failures = 0
    for cycle in range(args.cycles):
        try:
            predictions = fetch(SOURCES["predictions"])
            results = fetch(SOURCES["results"])
            report = observe_cycle(args.workspace, predictions=predictions, results=results,
                                   received_at=datetime.now(timezone.utc).isoformat(), plan=plan)
            print(json.dumps({"event": "public_shadow_cycle", **report}, ensure_ascii=False), flush=True)
            learned = forecast_learning_cycle(args.workspace, predictions=predictions, results=results,
                                              received_at=datetime.now(timezone.utc).isoformat())
            print(json.dumps({"event": "public_forecast_learning", **learned}), flush=True)
            failures = 0
        except (OSError, TimeoutError) as exc:
            failures += 1
            print(json.dumps({"event": "public_source_unavailable", "error_type": type(exc).__name__,
                              "consecutive_failures": failures}), flush=True)
            if failures >= 5:
                raise RuntimeError("public observer stopped after bounded source failures; checkpoints retained") from None
        if cycle + 1 < args.cycles:
            time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
