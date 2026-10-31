import pytest

from limitara.sample_run import SCENARIOS, report, run_all


@pytest.fixture(scope="module")
def scenarios():
    return run_all()


def test_there_are_at_least_ten_enquiries_and_all_behave_as_designed(scenarios):
    assert len(scenarios) >= 10
    assert [s.no for s in scenarios if not s.passed] == []


def test_not_one_enquiry_reached_a_customer_or_became_an_order_without_a_person(scenarios):
    for s in scenarios:
        assert s.outcome["customer_mail"] == 0, s.no
        assert s.outcome["orders"] == 0, s.no


def test_every_quote_made_waits_for_approval_or_for_a_person_to_resolve_a_block(scenarios):
    for s in scenarios:
        for q in s.outcome["quotes"]:
            assert q["status"] in ("pending_approval", "draft"), (s.no, q["status"])
            assert q["status"] != "draft" or any(f["severity"] == "block" for f in q["flags"])


def test_every_price_traces_to_a_plan_and_flags_explain_every_exception(scenarios):
    by_no = {s.no: s for s in scenarios}
    q = by_no[8].outcome["quotes"][0]
    assert any(f["code"] == "TERMS_BREACH_FLOOR" and "Lowest price meeting the floor" in f["detail"] for f in q["flags"])
    assert all(q["plan"] for s in scenarios for q in s.outcome["quotes"])


def test_the_what_if_changes_the_plan_and_names_what_did_not_change(scenarios):
    extra = {s.no: s.extra for s in scenarios}[12]
    assert "Changed:" in extra and "Unchanged:" in extra


def test_the_report_is_written_from_the_systems_own_records(scenarios):
    text = report(scenarios)
    assert text.count("\n## ") == 15 and "Rs 1187.50" in text and "as designed" in text
