"""AI panel: parsing, aggregation, prompt cap and the fake gateway."""

from __future__ import annotations

import json

from penny.ai.panel import (ModelForecast, aggregate, build_messages, parse_forecast,
                            price_forecast_text, reaction_text)

VALID = json.dumps({
    "strength": "strong", "direction": "up", "shape": "grinder",
    "durability": "sustained", "confidence": 0.7, "catalyst": "news",
    "sentiment": 0.3,
    "forecast": {"15m": {"price": 1.2}, "60m": {"price": 1.4},
                 "session_end": {"price": 1.8}},
    "expected_peak": 1.9, "expected_low": 1.1, "market_effect": "risk-on",
    "reasons": ["a", "b"], "flags": ["dilution"], "change_view": "volume fades",
})


def test_parser_accepts_valid_object():
    fc = parse_forecast("m", VALID)
    assert fc.valid
    assert fc.forecast["60m"] == 1.4
    assert fc.direction == "up"
    assert fc.flags == ["dilution"]


def test_parser_strips_code_fences():
    assert parse_forecast("m", f"```json\n{VALID}\n```").valid


def test_parser_rejects_html_and_garbage():
    assert not parse_forecast("m", "<html>not json</html>").valid
    assert not parse_forecast("m", "no json here").valid


def test_parser_maps_unknown_enums_and_drops_bad_prices():
    fc = parse_forecast("m", json.dumps({"direction": "sideways",
                                         "forecast": {"15m": {"price": -3}}}))
    assert fc.direction == "unclear"
    assert not fc.valid


def test_aggregation_majority_and_median():
    many = [ModelForecast(model=f"m{i}", valid=True, direction="up", shape="grinder",
                          strength="strong", durability="sustained", confidence=0.6,
                          forecast={"15m": 1.1 + i * 0.1, "60m": 1.2, "session_end": 1.3},
                          reasons=[f"r{i}"], flags=["dilution"] if i == 0 else [])
            for i in range(3)]
    many[1].direction = "down"
    agg = aggregate(many)
    assert agg.direction == "up"
    assert agg.n_models == 3
    assert abs(agg.forecast["15m"] - 1.2) < 1e-9
    assert "dilution" in agg.flags


def test_aggregation_with_no_valid_models():
    agg = aggregate([ModelForecast(model="x", valid=False)])
    assert agg.n_models == 0
    assert agg.validated is False


def test_prompt_cap():
    facts = {"symbol": "X", "price": 1.23456789,
             "news": [{"title": "t" * 500, "summary": "s" * 500}],
             "reasons": ["r" * 500]}
    messages = build_messages(facts)
    assert len(messages[1]["content"]) <= 7000


def test_reaction_and_price_text():
    agg = aggregate([ModelForecast(model="m", valid=True, direction="up", shape="grinder",
                                   strength="strong", durability="sustained", confidence=0.7,
                                   forecast={"15m": 1.2, "60m": 1.4, "session_end": 1.8})])
    assert "REACTION" in reaction_text(agg)
    assert "PRICE FORECAST" in price_forecast_text(agg, 1.0)


def test_fake_gateway_biases():
    from penny.ai.gateway import FakeGateway
    from penny.config import Config
    import tempfile
    from pathlib import Path
    cfg = Config(Path(tempfile.mkdtemp()) / "c.env")
    gw = FakeGateway(cfg)
    facts = {"symbol": "X", "price": 1.0, "rise15": 0.05, "volx15": 8.0}
    messages = build_messages(facts)
    good = parse_forecast("accurate", gw.chat("accurate", messages).text)
    bad = parse_forecast("wrong", gw.chat("wrong", messages).text)
    assert good.valid and bad.valid
    assert good.forecast["15m"] > 1.0
    assert bad.forecast["15m"] < 1.0
