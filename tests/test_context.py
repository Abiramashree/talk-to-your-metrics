"""Lint for the context the agent reads: every metric and dimension explained, no dangling or ambiguous names.
Fails when the semantic model changes and its descriptions or synonyms no longer fit it."""

import re
from collections import defaultdict

import pytest


@pytest.fixture(scope="module")
def context(layer):
    metrics = layer.list_metrics()["metrics"]
    dimensions = {}
    for m in metrics:
        for d in layer.get_dimensions([m["name"]])["dimensions"]:
            dimensions[d["name"]] = d
    return metrics, dimensions


def test_every_metric_has_a_label_and_a_description(context):
    metrics, _ = context
    assert [m["name"] for m in metrics if not m.get("label") or len(m.get("description") or "") < 20] == []


def test_every_dimension_has_a_description_and_values(context):
    _, dimensions = context
    assert [n for n, d in dimensions.items() if not d.get("description") or not d.get("values")] == []


def test_descriptions_name_only_dimensions_that_exist(context):
    metrics, dimensions = context
    dangling = [(m["name"], ref) for m in metrics for ref in re.findall(r"\b[a-z_]+__[a-z_]+\b", m["description"])
                if ref not in dimensions and not ref.startswith("metric_time")]
    assert dangling == []


def test_no_synonym_points_to_two_metrics_or_shadows_another_metric(context):
    metrics, _ = context
    names = {m["name"].replace("_", " ") for m in metrics}
    owners = defaultdict(list)
    for m in metrics:
        for s in m.get("synonyms") or []:
            owners[s.strip().lower()].append(m["name"])
    shared = {s: o for s, o in owners.items() if len(o) > 1}
    shadowing = {s: o for s, o in owners.items() if s in names and s != o[0].replace("_", " ")}
    assert shared == {} and shadowing == {}
