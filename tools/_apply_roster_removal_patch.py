from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exact replacement count 1, got {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


source = Path("src/autosport/betfair_historical_import.py")
replace_once(
    source,
    '''            prior_definition: dict[str, Any] | None = None
            market_definition = change.get("marketDefinition")
''',
    '''            prior_definition: dict[str, Any] | None = None
            removed_runner_ids: set[str] = set()
            market_definition = change.get("marketDefinition")
''',
)
replace_once(
    source,
    '''                    declared_runner_ids[market_id] = roster
''',
    '''                    previous_roster = declared_runner_ids.get(market_id)
                    if previous_roster is not None:
                        removed_runner_ids = previous_roster.difference(roster)
                    declared_runner_ids[market_id] = roster
''',
)
replace_once(
    source,
    '''            market_status = str(definition.get("status") or "").upper()
            current_bet_delay = _bet_delay_seconds(definition, path=path, line_number=line_number)
            if prior_definition is not None:
''',
    '''            market_status = str(definition.get("status") or "").upper()
            current_bet_delay = _bet_delay_seconds(definition, path=path, line_number=line_number)
            if removed_runner_ids:
                names = runner_names.get(market_id, {})
                event_id = str(definition.get("eventId") or "").strip()
                if not event_id:
                    raise ValueError(
                        f"{path}: line {line_number} supported Betfair market requires source eventId"
                    )
                for selection_id in sorted(removed_runner_ids):
                    key = (market_id, selection_id)
                    previous_price = last_visible_price.get(key)
                    previous_metadata = last_visible_metadata.get(key, {})
                    # An authoritative roster removal invalidates every cached execution
                    # ladder for that selection. A later re-add starts from an empty,
                    # unverified book until the provider supplies fresh image evidence.
                    available_books.pop(key, None)
                    if previous_price is not None and market_status != "CLOSED":
                        provider_field = str(
                            previous_metadata.get("provider_price_field") or "marketDefinition.runners"
                        )
                        metadata = _base_metadata(definition, names, selection_id)
                        metadata.update(
                            {
                                "price_semantics": "betfair_runner_roster_removed",
                                "provider_price_field": provider_field,
                                "execution_quote_verified": False,
                                "actual_fill_verified": False,
                                "paper_fill_eligible": False,
                                "paper_fill_eligibility_reason": (
                                    "selection was removed from the authoritative marketDefinition.runners roster"
                                ),
                                "paper_fill_capacity_verified": False,
                                "paper_fill_capacity_unit_bound": False,
                                "betfair_market_status": market_status or "UNKNOWN",
                                "betfair_bet_delay_seconds": current_bet_delay,
                                "market_definition_transition": True,
                                "runner_roster_membership": False,
                                "runner_removed_from_authoritative_roster": True,
                            }
                        )
                        append_event(
                            path=path,
                            event_id=event_id,
                            market_id=market_id,
                            selection_id=selection_id,
                            odds=previous_price,
                            observed=observed,
                            canonical_market_type=canonical_market_type,
                            status=(market_status.lower() if market_status else "unknown"),
                            metadata=metadata,
                        )
                    # append_event tracks visible quotes by design; roster removal is an
                    # invalidation marker, not a quote that may seed later transitions.
                    last_visible_price.pop(key, None)
                    last_visible_metadata.pop(key, None)

            if prior_definition is not None:
''',
)
replace_once(
    source,
    '''                "runner_roster_membership_verified": True,
                "paper_fill_capacity_enforced": False,
''',
    '''                "runner_roster_membership_verified": True,
                "runner_roster_removal_invalidates_cached_quotes": True,
                "paper_fill_capacity_enforced": False,
''',
)
replace_once(
    source,
    '''            "definition_state_transitions_preserved": True,
            "suspended_or_non_open_intervals_preserved": False,
''',
    '''            "definition_state_transitions_preserved": True,
            "runner_roster_removals_preserved": True,
            "suspended_or_non_open_intervals_preserved": False,
''',
)

truth = Path("src/autosport/price_truth.py")
replace_once(
    truth,
    '''        "betfair_available_to_back_unavailable",
        "betfair_market_definition_state_transition",
''',
    '''        "betfair_available_to_back_unavailable",
        "betfair_market_definition_state_transition",
        "betfair_runner_roster_removed",
''',
)

tests = Path("tests/test_betfair_available_back_import.py")
replace_once(
    tests,
    '''from autosport.price_truth import paper_quote_rejection_reason
''',
    '''from autosport.price_truth import paper_quote_rejection_reason
from autosport.storage import SQLiteMarketStore
''',
)
marker = '''    def test_explicit_malformed_runner_roster_fails_closed_before_membership_reuse(self) -> None:
'''
addition = '''    def test_authoritative_runner_removal_invalidates_current_quote(self) -> None:
        lines = _pro_stream()
        lines[0]["mc"][0]["marketDefinition"]["runners"].append(
            {"id": 999, "name": "Removed Runner", "status": "ACTIVE"}
        )
        lines[0]["mc"][0]["rc"].append({"id": 999, "atb": [[2.2, 25.0]]})
        lines.insert(
            1,
            {
                "op": "mcm",
                "pt": _epoch_ms("2026-02-10T12:02:00Z"),
                "mc": [
                    {
                        "id": "1.advanced",
                        "marketDefinition": {
                            "runners": [
                                {"id": 101, "name": "Player A", "status": "ACTIVE"}
                            ]
                        },
                    }
                ],
            },
        )
        lines[-1]["mc"][0]["marketDefinition"]["runners"].append(
            {"id": 999, "name": "Removed Runner", "status": "REMOVED"}
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output, events = self._import(root, lines)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            removed = [
                event
                for event in events
                if event.selection_id == "999" and event.observed_ts == "2026-02-10T12:02:00Z"
            ]
            self.assertEqual(len(removed), 1)
            event = removed[0]
            self.assertEqual(event.metadata["price_semantics"], "betfair_runner_roster_removed")
            self.assertIs(event.metadata["runner_roster_membership"], False)
            self.assertIs(event.metadata["runner_removed_from_authoritative_roster"], True)
            self.assertIs(event.metadata["execution_quote_verified"], False)
            self.assertIs(event.metadata["paper_fill_eligible"], False)
            self.assertIn("not a verified executable quote", paper_quote_rejection_reason(event, "1"))

            store = SQLiteMarketStore(root / "projection.db")
            try:
                store.append_many(events)
                current = store.current()[event.quote_key]
            finally:
                store.close()
            self.assertEqual(current.observed_ts, "2026-02-10T12:02:00Z")
            self.assertEqual(current.metadata["price_semantics"], "betfair_runner_roster_removed")

        availability = manifest["governance"]["availability_semantics"]
        self.assertIs(availability["runner_roster_removals_preserved"], True)
        price_truth = manifest["governance"]["price_semantics"]
        self.assertIs(price_truth["runner_roster_removal_invalidates_cached_quotes"], True)

    def test_runner_readd_cannot_resurrect_pre_removal_verified_ladder(self) -> None:
        lines = _pro_stream()
        lines[0]["mc"][0]["marketDefinition"]["runners"].append(
            {"id": 999, "name": "Removed Runner", "status": "ACTIVE"}
        )
        lines[0]["mc"][0]["rc"].append({"id": 999, "atb": [[2.2, 25.0]]})
        lines.insert(
            1,
            {
                "op": "mcm",
                "pt": _epoch_ms("2026-02-10T12:02:00Z"),
                "mc": [
                    {
                        "id": "1.advanced",
                        "marketDefinition": {
                            "runners": [
                                {"id": 101, "name": "Player A", "status": "ACTIVE"}
                            ]
                        },
                    }
                ],
            },
        )
        lines.insert(
            2,
            {
                "op": "mcm",
                "pt": _epoch_ms("2026-02-10T12:03:00Z"),
                "mc": [
                    {
                        "id": "1.advanced",
                        "marketDefinition": {
                            "runners": [
                                {"id": 101, "name": "Player A", "status": "ACTIVE"},
                                {"id": 999, "name": "Removed Runner", "status": "ACTIVE"},
                            ]
                        },
                        "rc": [{"id": 999, "atb": [[2.1, 10.0]]}],
                    }
                ],
            },
        )
        lines[-1]["mc"][0]["marketDefinition"]["runners"].append(
            {"id": 999, "name": "Removed Runner", "status": "REMOVED"}
        )

        with tempfile.TemporaryDirectory() as tmp:
            _output, events = self._import(Path(tmp), lines)

        readded = [
            event
            for event in events
            if event.selection_id == "999" and event.observed_ts == "2026-02-10T12:03:00Z"
        ]
        self.assertEqual(len(readded), 1)
        event = readded[0]
        self.assertEqual(event.decimal_odds, Decimal("2.1"))
        self.assertEqual(event.metadata["paper_fill_available_size"], "10.0")
        self.assertIs(event.metadata["execution_quote_verified"], False)
        self.assertIs(event.metadata["paper_fill_eligible"], False)

'''
replace_once(tests, marker, addition + marker)
