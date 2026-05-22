from __future__ import annotations

import json

from app.services.listing_service import normalize_listing_row


def test_ourdomain_display_name_prefers_building_and_unit():
    row = {
        "id": "od_307195",
        "name": "#6045 - Ground Floor, Courtyard View, 22m²",
        "source": "ourdomain",
        "city": "Amsterdam Diemen",
        "features": json.dumps([
            "Unit: #6045",
            "Building: Amsterdam Diemen",
            "Area: 22 m²",
            "Detail: Ground Floor, Courtyard View",
        ]),
    }

    normalized = normalize_listing_row(row)

    assert normalized["name"] == "Diemen #6045"
    assert "22m²" not in normalized["name"]


def test_ourdomain_display_name_leaves_non_unit_names_alone():
    row = {
        "id": "od-manual",
        "name": "Amsterdam Diemen",
        "source": "ourdomain",
        "city": "Amsterdam Diemen",
        "features": "[]",
    }

    assert normalize_listing_row(row)["name"] == "Amsterdam Diemen"
