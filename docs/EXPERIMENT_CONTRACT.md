# Experiment contract

A reproducible experiment binds exact dataset/event-range identity, replay mode/speed, bankroll/currency, strategy/agent/model versions, configuration, random seed(s) where applicable, software source SHA and output decision/settlement/evaluation records. A causal experiment forbids future event/result access. Re-running the same deterministic experiment should reproduce event ordering and deterministic outputs; stochastic models record seeds/distributions and are evaluated statistically rather than falsely claimed bit-identical.
