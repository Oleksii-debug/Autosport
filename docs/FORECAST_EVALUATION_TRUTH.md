# Forecast / evaluation truth contract

Autosport forecast evaluation is a causal research path. It is not a claim of betting profitability and it does not enable real-money execution.

## Forecast identity

Every new versioned forecast is represented by `ForecastRecord` and carries:

- `forecast_id`;
- canonical `quote_key`;
- probability;
- model id and model version;
- strategy version;
- model training cutoff timestamp;
- input/evidence cutoff timestamp;
- forecast generation timestamp;
- uncertainty;
- evidence hashes;
- optional market snapshot hash;
- provenance metadata;
- canonical SHA-256 hash.

The required causal order is:

`model_training_cutoff_ts <= input_cutoff_ts <= generated_at`

Forecast provenance rejects result/outcome/future-quote keys. Outcome facts live in a separate post-outcome structure and never in the pre-outcome forecast ledger.

## Paper strategy

`PaperValueAgent` remains paper-only. It accepts both the legacy minimal `Forecast` contract and the new `ForecastRecord` contract. A forecast whose `as_of_ts` is later than the market event timestamp cannot be used.

For a `ForecastRecord`, every opened paper decision records the forecast id, canonical forecast hash, model/strategy versions, training/input cutoffs, uncertainty, evidence hashes and market snapshot hash in the append-only decision ledger.

## Holdout and walk-forward evaluation

`TemporalEvaluationWindow` separates the end of the model-training period from the validation/holdout period. A forecast generated in an evaluation window is rejected if its `model_training_cutoff_ts` is later than that window's `training_end_ts`.

Multiple non-overlapping temporal windows form walk-forward evaluation. Overlapping folds fail closed.

Evaluation requires post-forecast outcome facts and reports:

- forecast count;
- Brier score;
- log loss;
- mean stated uncertainty;
- calibration bins;
- exact model versions represented;
- exact strategy versions represented.

No evaluation result changes `REAL_MONEY_EXECUTION=false`. A good historical metric is evidence about a particular dataset/model/version/window, not a guarantee of future profit.
