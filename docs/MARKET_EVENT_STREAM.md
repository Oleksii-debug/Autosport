# MarketEventStream contract

Downstream strategy/portfolio consumers receive a chronological stream of canonical MarketEvents independent of whether the producer is live observation, historical replay or a test fixture. Producers may have different transport/reconnect behavior, but consumers must not branch on provider HTML or read hidden future replay buffers. Replay producers expose only causally released events. A future async interface will add bounded backpressure/cancellation while preserving deterministic event identity/order semantics.
