"""Research-only team residual learner anchored to pre-match market probabilities.

The three logits receive (+home_weight-away_weight, 0, -home_weight+away_weight).
Cross-entropy updates use revealed results, with shrinkage and bounded weights.
This supplies canonical ForecastRecord values, not deployment or betting authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Context, Decimal, localcontext
import hashlib
import json

from .forecasting import ForecastRecord, parse_iso_timestamp


OUTCOMES = ("1", "X", "2")
_ARITHMETIC = Context(prec=28)


def _text(value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > 256:
        raise ValueError("identity must be bounded canonical text")
    if any(ord(c) < 32 for c in value):
        raise ValueError("identity contains control characters")
    return value


def _digest(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _decimal(value: object) -> Decimal:
    if type(value) is not str or len(value) > 64:
        raise ValueError("expected bounded decimal text")
    number = Decimal(value)
    if not number.is_finite() or abs(number) > 1000000 or number.as_tuple().exponent < -100:
        raise ValueError("decimal is outside the model envelope")
    return number


@dataclass(frozen=True, slots=True)
class TeamResidualConfig:
    learning_rate: str = "0.02"
    regularization: str = "0.01"
    maximum_weight: str = "0.5"
    minimum_team_observations: int = 5

    def __post_init__(self) -> None:
        for name in ("learning_rate", "regularization", "maximum_weight"):
            if not 0 < _decimal(getattr(self, name)) <= 1:
                raise ValueError(f"{name} must be in (0,1]")
        if (type(self.minimum_team_observations) is not int
                or not 1 <= self.minimum_team_observations <= 10000):
            raise ValueError("minimum team observations must be in 1..10000")

    def payload(self) -> dict[str, object]:
        return {key: getattr(self, key) for key in self.__slots__}


@dataclass(frozen=True, slots=True)
class TeamMarketInput:
    match_id: str
    home_id: str
    away_id: str
    available_at: str
    market_probabilities: tuple[Decimal, Decimal, Decimal]
    source_sha256: str

    def __post_init__(self) -> None:
        for value in (self.match_id, self.home_id, self.away_id):
            _text(value)
        if self.home_id == self.away_id:
            raise ValueError("home and away must identify different teams")
        parse_iso_timestamp(self.available_at)
        if type(self.market_probabilities) is not tuple or len(self.market_probabilities) != 3:
            raise ValueError("exact three-way probability tuple required")
        for p in self.market_probabilities:
            if type(p) is not Decimal or not p.is_finite() or not 0 < p < 1:
                raise ValueError("probabilities must be finite Decimal values in (0,1)")
        with localcontext(_ARITHMETIC):
            if abs(sum(self.market_probabilities) - 1) > Decimal("1e-20"):
                raise ValueError("market probabilities must sum to one")
        if (type(self.source_sha256) is not str or len(self.source_sha256) != 64
                or any(c not in "0123456789abcdef" for c in self.source_sha256)):
            raise ValueError("source SHA-256 required")

    def payload(self) -> dict[str, object]:
        return {"match_id": self.match_id, "home_id": self.home_id, "away_id": self.away_id,
                "available_at": self.available_at, "source_sha256": self.source_sha256,
                "market_probabilities": [str(p) for p in self.market_probabilities]}


class TeamResidualModel:
    """Small deterministic learner; no store, scheduler, Factory or stake control."""

    def __init__(self, config: TeamResidualConfig, *, created_at: str):
        if type(config) is not TeamResidualConfig:
            raise TypeError("exact TeamResidualConfig required")
        config.__post_init__()
        parse_iso_timestamp(created_at)
        self.config = config
        self.created_at = self.training_cutoff = created_at
        self.weights: dict[str, Decimal] = {}
        self.counts: dict[str, int] = {}
        self.seen: set[str] = set()
        self.training_sha256 = _digest({"config": config.payload(), "created_at": created_at})

    def _probabilities(self, item: TeamMarketInput) -> tuple[Decimal, Decimal, Decimal]:
        with localcontext(_ARITHMETIC):
            delta = self.weights.get(item.home_id, Decimal(0)) - self.weights.get(item.away_id, Decimal(0))
            home, draw, away = item.market_probabilities
            raw = (home * delta.exp(), draw, away * (-delta).exp())
            total = sum(raw)
            first, second = raw[0] / total, raw[1] / total
            return first, second, Decimal(1) - first - second

    def supported(self, item: TeamMarketInput) -> bool:
        return all(self.counts.get(team, 0) >= self.config.minimum_team_observations
                   for team in (item.home_id, item.away_id))

    def predict(self, item: TeamMarketInput) -> tuple[Decimal, Decimal, Decimal]:
        if type(item) is not TeamMarketInput:
            raise TypeError("exact TeamMarketInput required")
        item.__post_init__()
        if parse_iso_timestamp(self.training_cutoff) >= parse_iso_timestamp(item.available_at):
            raise ValueError("training must be available strictly before prediction")
        if item.match_id in self.seen:
            raise ValueError("cannot forecast a match already used for training")
        return self._probabilities(item) if self.supported(item) else item.market_probabilities

    def learn(self, item: TeamMarketInput, *, result: str, available_at: str) -> None:
        if type(item) is not TeamMarketInput:
            raise TypeError("exact TeamMarketInput required")
        item.__post_init__()
        if type(result) is not str or result not in OUTCOMES:
            raise ValueError("result class must be 1, X or 2")
        at = parse_iso_timestamp(available_at)
        if at <= parse_iso_timestamp(item.available_at):
            raise ValueError("result must be revealed after the input")
        if at < parse_iso_timestamp(self.training_cutoff):
            raise ValueError("training observations must be availability ordered")
        if item.match_id in self.seen:
            raise ValueError("duplicate match cannot update the model twice")
        # Compute all changes before mutating state.
        with localcontext(_ARITHMETIC):
            home, _, away = self._probabilities(item)
            gradient = home - away - (int(result == "1") - int(result == "2"))
            rate = Decimal(self.config.learning_rate)
            penalty = Decimal(self.config.regularization)
            bound = Decimal(self.config.maximum_weight)
            updates = {}
            for team, sign in ((item.home_id, 1), (item.away_id, -1)):
                old = self.weights.get(team, Decimal(0))
                updated = old - rate * (sign * gradient + penalty * old)
                updates[team] = max(-bound, min(bound, updated))
        digest = _digest({"previous": self.training_sha256, "input": item.payload(),
                          "result": result, "available_at": available_at})
        self.weights.update(updates)
        for team in updates:
            self.counts[team] = self.counts.get(team, 0) + 1
        self.seen.add(item.match_id)
        self.training_cutoff, self.training_sha256 = available_at, digest

    def payload(self) -> dict[str, object]:
        return {"schema": "autosport.team_residual_model", "schema_version": 1,
                "config": self.config.payload(), "created_at": self.created_at,
                "training_cutoff": self.training_cutoff, "training_sha256": self.training_sha256,
                "weights": {k: str(v) for k, v in sorted(self.weights.items())},
                "counts": dict(sorted(self.counts.items())), "seen": sorted(self.seen),
                "research_only": True, "promotion_authorized": False,
                "real_money_execution": False}

    @classmethod
    def restore(cls, payload: dict) -> "TeamResidualModel":
        expected = {"schema", "schema_version", "config", "created_at", "training_cutoff",
                    "training_sha256", "weights", "counts", "seen", "research_only",
                    "promotion_authorized", "real_money_execution"}
        if type(payload) is not dict or set(payload) != expected:
            raise ValueError("model checkpoint fields mismatch")
        if (payload["schema"] != "autosport.team_residual_model"
                or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
                or payload["research_only"] is not True or payload["promotion_authorized"] is not False
                or payload["real_money_execution"] is not False):
            raise ValueError("model checkpoint boundary mismatch")
        model = cls(TeamResidualConfig(**payload["config"]), created_at=payload["created_at"])
        if type(payload["weights"]) is not dict or type(payload["counts"]) is not dict:
            raise ValueError("model weights and counts must be objects")
        if type(payload["seen"]) is not list:
            raise ValueError("seen matches must be a list")
        model.seen = {_text(v) for v in payload["seen"]}
        if len(model.seen) != len(payload["seen"]):
            raise ValueError("duplicate seen match")
        model.weights = {_text(k): _decimal(v) for k, v in payload["weights"].items()}
        model.counts = dict(payload["counts"])
        if set(model.counts) != set(model.weights):
            raise ValueError("team counts and weights differ")
        if (any(type(n) is not int or n <= 0 for n in model.counts.values())
                or sum(model.counts.values()) != 2 * len(model.seen)
                or any(n > len(model.seen) for n in model.counts.values())
                or any(abs(v) > Decimal(model.config.maximum_weight) for v in model.weights.values())):
            raise ValueError("invalid model weight/count envelope")
        if parse_iso_timestamp(payload["training_cutoff"]) < parse_iso_timestamp(model.created_at):
            raise ValueError("model cutoff predates creation")
        sha = payload["training_sha256"]
        if type(sha) is not str or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("invalid training digest")
        model.training_cutoff, model.training_sha256 = payload["training_cutoff"], sha
        if model.payload() != payload:
            raise ValueError("checkpoint is not canonical")
        return model

    def forecasts(self, item: TeamMarketInput) -> tuple[ForecastRecord, ...]:
        probabilities = self.predict(item)
        version = _digest(self.payload())
        input_hash = _digest(item.payload())
        return tuple(ForecastRecord(
            quote_key=f"{item.match_id}|1x2|{pick}", probability=probability,
            model_id="team-market-residual-v1", model_version=version,
            strategy_version="team-residual-research-v1",
            model_training_cutoff_ts=self.training_cutoff,
            input_cutoff_ts=item.available_at, generated_at=item.available_at,
            uncertainty=Decimal(1), evidence_hashes=(self.training_sha256, input_hash),
            market_snapshot_hash=input_hash,
            provenance={"research_only": True, "team_history_supported": self.supported(item),
                        "uncertainty_calibrated": False, "promotion_authorized": False},
            forecast_id=_digest({"model": version, "input": input_hash, "pick": pick}),
        ) for pick, probability in zip(OUTCOMES, probabilities, strict=True))
