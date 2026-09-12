# Integration policy

Feature branches must converge into `main` through reviewable PRs. Before merge, atomically re-read PR head/base and exact CI; stale green evidence does not transfer to a changed SHA. Prefer repairing/incorporating the incumbent useful branch over opening duplicates. After integration, downstream workers must refresh assumptions from new `main` rather than continue from old reports.
