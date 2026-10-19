from scope.agents.capability import guard
from scope.agents.estimator import build_estimate, sanitize
from scope.agents.extractor import finalize, heuristic_extract, merge
from scope.config import SAMPLES_DIR
from scope.models import CapabilityCheck, ExtractedRequirement, Extraction, Requirement, TaskDraft
from scope.text import parse_front_matter, parse_turns, quote_in_transcript, segment

KICKOFF = parse_front_matter((SAMPLES_DIR / "01_brightsmile_kickoff.md").read_text())[1]


def test_samples_include_three_meetings_and_a_follow_up() -> None:
    metas = [parse_front_matter(f.read_text())[0] for f in sorted(SAMPLES_DIR.glob("*.md"))]
    assert len(metas) >= 3
    assert any(m["kind"] == "follow_up" for m in metas)


def test_parse_turns_reads_speaker_and_role() -> None:
    turns = parse_turns(KICKOFF)
    assert turns[0].speaker == "Anil" and turns[0].role == "Consultant"
    assert any(t.speaker == "Sarah" and "Client" in t.role for t in turns)


def test_quote_verification_rejects_invented_quotes() -> None:
    assert quote_in_transcript("We use Stripe for everything else.", KICKOFF)
    assert quote_in_transcript("we use stripe for everything else", KICKOFF)  # case/punctuation-insensitive
    assert not quote_in_transcript("We need a blockchain loyalty token for patients.", KICKOFF)
    assert not quote_in_transcript("ok", KICKOFF)


def test_segmentation_splits_long_text_on_lines_with_overlap() -> None:
    text = "\n".join(f"Speaker: line {i} " + "x" * 80 for i in range(300))
    parts = segment(text, 5000, overlap=500)
    assert len(parts) > 4
    assert all(len(p) <= 5600 for p in parts)
    assert parts[1].splitlines()[0].startswith("Speaker: line")  # starts on a line boundary
    assert segment("short", 5000) == ["short"]


def test_offline_extraction_finds_requirements_and_flags_vague_ones() -> None:
    reqs, items = finalize(heuristic_extract(KICKOFF), KICKOFF)
    assert len(reqs) >= 8
    assert all(r.quote_verified for r in reqs)
    assert any(r.ambiguous and "better" in r.description for r in reqs)
    assert items["decisions"] and items["dependencies"] and items["open_questions"]


def test_merge_deduplicates_requirements_across_segments() -> None:
    a = ExtractedRequirement(title="Stripe payments", description="Members pay with Stripe monthly", source_quote="q")
    b = ExtractedRequirement(title="Stripe payments", description="Members pay with Stripe monthly or annually", source_quote="q")
    c = ExtractedRequirement(title="Reviews", description="Patients leave reviews with moderation", source_quote="q")
    merged = merge([Extraction(requirements=[a]), Extraction(requirements=[b, c])])
    assert [r.title for r in merged.requirements] == ["Stripe payments", "Reviews"]
    assert merged.requirements[0].description.endswith("annually")


def test_guard_never_allows_support_without_evidence() -> None:
    assert guard("supported_by_configuration", {"answered": False, "citations": []})[0] == "custom_development"
    assert guard("partially_supported", {"answered": True, "citations": []})[0] == "custom_development"
    assert (
        guard("supported_by_configuration", {"answered": True, "citations": [{"title": "x"}]})[0] == "supported_by_configuration"
    )


def test_sanitize_orders_and_clamps_hours() -> None:
    t = sanitize(TaskDraft(title="x", low=10, likely=2, high=5), "custom_development")
    assert (t.low, t.likely, t.high) == (2, 5, 10)
    t = sanitize(TaskDraft(title="config", low=10, likely=40, high=100), "supported_by_configuration")
    assert t.high == 16 and "clamped" in t.notes


def test_estimate_adds_qa_and_pm_and_sums() -> None:
    reqs = [
        Requirement(id="R1", title="A", description="a", source_quote="q"),
        Requirement(id="R2", title="B", description="b", source_quote="q"),
    ]
    checks = {
        "R1": CapabilityCheck(
            requirement_id="R1", status="supported_by_configuration", rationale="", brain_question="", brain_answered=True
        ),
        "R2": CapabilityCheck(
            requirement_id="R2", status="needs_clarification", rationale="", brain_question="", brain_answered=False
        ),
    }
    est = build_estimate(reqs, checks, {}, "heuristic")
    titles = [t.title for t in est.tasks]
    assert "QA, UAT and launch checks" in titles and "Project management" in titles
    assert est.totals.likely == round(sum(t.likely for t in est.tasks), 1)
    assert est.by_requirement["R1"].likely == 2
    assert any("R2" in n for n in est.notes)
