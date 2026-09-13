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
            removed_runner_quotes: list[tuple[str, str, dict[str, Any], str | None]] = []
            market_definition = change.get("marketDefinition")
''',
)
replace_once(
    source,
    '''                    previous_roster = declared_runner_ids.get(market_id, set())
                    for removed_selection_id in previous_roster.difference(roster):
                        removed_key = (market_id, removed_selection_id)
                        available_books.pop(removed_key, None)
                        last_visible_price.pop(removed_key, None)
                        last_visible_metadata.pop(removed_key, None)
                        names.pop(removed_selection_id, None)
                    declared_runner_ids[market_id] = roster
''',
    '''                    previous_roster = declared_runner_ids.get(market_id, set())
                    for removed_selection_id in previous_roster.difference(roster):
                        removed_key = (market_id, removed_selection_id)
                        available_books.pop(removed_key, None)
                        previous_price = last_visible_price.pop(removed_key, None)
                        previous_metadata = last_visible_metadata.pop(removed_key, {})
                        previous_name = names.pop(removed_selection_id, None)
                        if previous_price is not None:
                            removed_runner_quotes.append(
                                (
                                    removed_selection_id,
                                    previous_price,
                                    previous_metadata,
                                    previous_name,
                                )
                            )
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
            if removed_runner_quotes and market_status != "CLOSED":
                event_id = str(definition.get("eventId") or "").strip()
                if not event_id:
                    raise ValueError(
                        f"{path}: line {line_number} supported Betfair market requires source eventId"
                    )
                current_names = runner_names.get(market_id, {})
                for selection_id, previous_price, previous_metadata, previous_name in removed_runner_quotes:
                    event_names = dict(current_names)
                    if previous_name:
                        event_names[selection_id] = previous_name
                    provider_field = str(
                        previous_metadata.get("provider_price_field") or "marketDefinition.runners"
                    )
                    metadata = _base_metadata(definition, event_names, selection_id)
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
                    # append_event updates the importer-visible quote cache; this marker
                    # is an invalidation, not a quote that may seed future transitions.
                    removed_key = (market_id, selection_id)
                    last_visible_price.pop(removed_key, None)
                    last_visible_metadata.pop(removed_key, None)

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

tests = Path("tests/test_betfair_roster_cache_invalidation.py")
replace_once(
    tests,
    '''from autosport.dataset import load_dataset
''',
    '''from autosport.dataset import load_dataset
from autosport.storage import SQLiteMarketStore
''',
)
replace_once(
    tests,
    '''        removed_transition_events = [
            event
            for event in events
            if event.selection_id == "999" and event.observed_ts == "2026-02-10T12:03:00Z"
        ]
''',
    '''        roster_removal_events = [
            event
            for event in events
            if event.selection_id == "999" and event.observed_ts == "2026-02-10T12:02:00Z"
        ]
        self.assertEqual(len(roster_removal_events), 1)
        removed = roster_removal_events[0]
        self.assertEqual(removed.metadata["price_semantics"], "betfair_runner_roster_removed")
        self.assertIs(removed.metadata["runner_roster_membership"], False)
        self.assertIs(removed.metadata["runner_removed_from_authoritative_roster"], True)
        self.assertIs(removed.metadata["execution_quote_verified"], False)
        self.assertIs(removed.metadata["paper_fill_eligible"], False)

        with tempfile.TemporaryDirectory() as projection_tmp:
            store = SQLiteMarketStore(Path(projection_tmp) / "market.db")
            try:
                store.append_many(
                    event
                    for event in events
                    if event.observed_ts <= "2026-02-10T12:02:00Z"
                )
                current = store.current()[removed.quote_key]
            finally:
                store.close()
        self.assertEqual(current.observed_ts, "2026-02-10T12:02:00Z")
        self.assertEqual(current.metadata["price_semantics"], "betfair_runner_roster_removed")

        removed_transition_events = [
            event
            for event in events
            if event.selection_id == "999" and event.observed_ts == "2026-02-10T12:03:00Z"
        ]
''',
)
