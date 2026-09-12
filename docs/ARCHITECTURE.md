# Autosport architecture map

```text
Permitted live provider / historical dataset
                |
                v
        Provider Adapter(s)
                |
                v
          MarketEventStream
                |
     +----------+----------+
     |                     |
     v                     v
Append-only history   Current MarketState
     |                     |
     v                     v
Replay/Evaluation     Dependency invalidation
     |                     |
     +----------+----------+
                |
                v
   Forecast / Strategy / Agent layer
                |
                v
      Candidate Ticket Generator
                |
                v
       Portfolio/Exposure Engine
                |
                v
             PaperBook
                |
                v
            Settlement
                |
                v
     Learning / Evaluation Records

All major states -> semantic visible/copyable Windows WebView UI
```

The arrows describe data authority, not thread/process ownership. High-frequency capture, persistence, calculations, agents and UI may run concurrently behind bounded queues. The user-facing shell must never become the authoritative market database; it is a projection of canonical services.
