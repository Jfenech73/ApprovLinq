from app.services.description_summary import summarise_total_invoice_description


def test_total_invoice_summary_keeps_concise_description():
    assert summarise_total_invoice_description("Office supplies") == "Office supplies"


def test_total_invoice_summary_uses_purchase_category_for_long_item_text():
    summary = summarise_total_invoice_description(
        "A very long item list that would otherwise be awkwardly truncated in the row table",
        "Sparkling water 24x500ml 12.00\nCoffee beans 1kg 18.50\nSoft drink cans 20.00",
    )
    assert summary == "food and beverage supplies"
    assert len(summary) < 80


def test_total_invoice_summary_strips_amounts_from_fallback_lines():
    summary = summarise_total_invoice_description(
        "Invoice goods or services",
        "Consultation labour hours 125.00\nMonthly maintenance support 75.00",
    )
    assert summary == "professional or business services"


def test_total_invoice_summary_falls_back_to_short_clean_item_phrase():
    summary = summarise_total_invoice_description(
        "",
        "Custom fabricated display panel 145.00\nInstallation bracket pack 23.50",
    )
    assert summary.startswith("Custom fabricated display panel")
    assert len(summary.split()) <= 14
