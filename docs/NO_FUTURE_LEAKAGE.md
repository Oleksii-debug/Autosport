# Future-leakage prohibition

Any run labeled causal/backtest/live-like must expose to strategies only information whose event time has been released. Dataset files may physically contain final outcomes, but the strategy-facing API must not. Feature generation, caches, joins and agent context construction must obey the same horizon. Tests should include deliberately tempting future fields and fail if they leak. Analysis-mode seeking may inspect future data only when the run is explicitly not counted as causal strategy evidence.
