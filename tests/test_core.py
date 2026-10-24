import os

import pytest

os.environ["GRAPH_NAME"] = "wingman_test"

from wingman import config, graph, recap, resolver  # noqa: E402

config.settings.cache_clear()


def test_norm_linkedin():
    assert graph.norm_linkedin("https://www.linkedin.com/in/Maya-O/?trk=x") == "https://www.linkedin.com/in/maya-o"
    assert graph.norm_linkedin("linkedin.com/in/someone") == "https://www.linkedin.com/in/someone"
    assert graph.norm_linkedin("https://www.linkedin.com/company/github") is None
    assert graph.person_id_for("https://linkedin.com/in/abc/") == "li:abc"


def test_name_scores():
    assert resolver.name_score("Casey Winters", "Casey Winters") == 1.0
    assert resolver.name_score("Casey", "Casey Winters") < resolver.AUTO_MIN  # first name only never auto-confirms
    assert resolver.name_score("Jon Smith", "Jon Clements") < 0.6
    s, why = resolver.score({"name": "Maya Okafor", "company": "Tessellate AI"},
                            {"name": "Maya Okafor", "company": "Tessellate AI", "is_speaker": True})
    assert s >= resolver.AUTO_MIN and "company matches" in why
    s2, why2 = resolver.score({"name": "Maya Okafor", "company": "Stripe"}, {"name": "Maya Okafor", "company": "Tessellate AI"})
    assert s2 < resolver.AUTO_MIN and "different company" in why2


def test_recap_windows():
    encs = [{"at": 1_000_000}, {"at": 1_300_000, "ended_at": 1_400_000}, {"at": 9_000_000}]
    w = recap._windows(encs)
    assert w[0][1] == 1_000_000 - recap.LEAD_IN_MS and w[0][2] == 1_300_000
    assert w[1][1] == 1_300_000 and w[1][2] == 1_400_000  # never overlaps the previous window
    assert w[2][2] == 9_000_000 + recap.MAX_ENCOUNTER_MS


@pytest.mark.asyncio
async def test_graph_queries_end_to_end():
    assert config.settings().graph_name == "wingman_test"
    try:
        await graph.g().delete()
    except Exception:
        pass
    await graph.ensure_schema()
    await graph.upsert_person({"id": graph.ME, "name": "Me", "is_me": True})
    await graph.upsert_person({"id": "li:priya", "name": "Priya Shah", "company": "Northwind",
                               "linkedin_url": "https://linkedin.com/in/priya", "is_speaker": True})
    await graph.upsert_person({"id": "k:dana", "name": "Dana Lee"})
    await graph.set_works_at(graph.ME, "Square", "Engineer", False, "extract")
    await graph.set_works_at("li:priya", "Square", "ML lead", False, "extract")
    await graph.set_works_at("li:priya", "Ledgerly", None, False, "extract")
    await graph.set_works_at("k:dana", "Ledgerly", None, False, "known")
    await graph.q("MATCH (me:Person {id:'me'}), (d:Person {id:'k:dana'}) MERGE (me)-[:KNOWS]->(d)")
    await graph.set_topics(graph.ME, ["developer productivity", "rust"], "extract")
    await graph.set_topics("li:priya", ["developer experience", "fraud detection"], "extract")
    from wingman import embed
    await embed.embed_missing_topics()
    await graph.add_fact("li:priya", "hook", "Posted about eval tooling", "LinkedIn post")

    cg = [c["text"] for c in await graph.common_ground("li:priya")]
    assert any("Square" in t for t in cg), cg
    assert any("Dana Lee" in t and "Ledgerly" in t for t in cg), cg
    assert any("developer experience" in t and "developer productivity" in t for t in cg), cg

    found = await graph.search_people("Priya Northwind")
    assert found and found[0]["id"] == "li:priya"
    assert (await graph.find_person_by_linkedin("https://www.linkedin.com/in/Priya/"))["id"] == "li:priya"

    eid = await graph.create_encounter(None, {"name": "Priya"})
    await graph.set_encounter_person(eid, "li:priya")
    raw = await graph.get_encounter_raw(eid)
    assert raw["person_id"] == "li:priya" and raw["status"] == "confirmed"
    await graph.add_note(eid, "send her the deck")
    assert (await graph.encounter_notes(eid))[0]["text"] == "send her the deck"
    await graph.replace_action_items(eid, "li:priya", [{"text": "Send deck", "owner": "me", "due": None,
                                                        "quote": "I'll send it"}])
    items = await graph.action_items(eid)
    assert items[0]["person_id"] == "li:priya" and items[0]["status"] == "open"
    assert await graph.open_items_for("li:priya") == ["Send deck"]
    upd = await graph.set_action_status(items[0]["id"], "done")
    assert upd["status"] == "done"
    assert (await graph.previous_encounters("li:priya"))[0]["id"] == eid
    header, rows = await graph.ro("MATCH (p:Person) RETURN count(p)")
    assert rows[0][0] == 3
    await graph.g().delete()


def test_same_person_first_name_only():
    from wingman import live
    assert live.same_person("Maya", "Maya Okafor")  # "I'm Maya" from the person you're talking to
    assert live.same_person("Maya Okafor", "Maya Okafor")
    assert not live.same_person("Mara", "Maya Okafor")
    assert not live.same_person("Dev Raman", "Maya Okafor")


def test_fixture_card_cites_by_text():
    from wingman import fixtures
    prompt = "Facts:\n[0] (profile) Previously at Stripe\n[1] (LinkedIn post) Open-sourced Tidepool\n"
    out = fixtures._cite({"common_ground": [{"text": "Stripe alumni", "cite": ["Stripe"]}],
                          "ask_about": [{"text": "Ask about Tidepool", "cite": ["Tidepool"]},
                                        {"text": "Unsupported", "cite": ["not in the facts"]}],
                          "goal_fit": [], "heads_up": [{"text": "thin", "match": ["nothing like this"]}]}, prompt)
    assert out["common_ground"][0]["fact_ids"] == [0]
    assert out["ask_about"][0]["fact_ids"] == [1]
    assert out["ask_about"][1]["fact_ids"] == []  # cards.build_card drops it, like an uncited claim
    assert out["heads_up"] == []


@pytest.mark.asyncio
async def test_business_search_falls_back_to_serp(monkeypatch):
    from wingman import brightdata

    async def not_enabled(*a, **k):
        raise brightdata.NotEnabled("403")

    monkeypatch.setattr(brightdata, "people_search", not_enabled)
    assert await resolver._people_candidates({"name": "Maya Okafor", "company": "Tessellate AI"}) == []


@pytest.mark.asyncio
async def test_shortlist_query_returns_one_row_per_speaker(monkeypatch):
    from wingman import cards, llm
    try:
        await graph.g().delete()
    except Exception:
        pass
    await graph.ensure_schema()
    await graph.upsert_person({"id": graph.ME, "name": "Me", "is_me": True})
    for pid, topic in (("li:a", "rust"), ("li:b", "llm evaluation"), ("li:c", "kubernetes")):
        await graph.upsert_person({"id": pid, "name": pid, "is_speaker": True})
        await graph.set_topics(pid, [topic], "extract")
    seen = {}

    async def fake_rank(me, people, limit):
        seen["people"] = people
        return None

    monkeypatch.setattr(llm, "rank_people", fake_rank)
    assert await cards.rank_shortlist(limit=3) == 3
    # FalkorDB treats a RETURN map that contains collect() as one group; this must stay one row per speaker
    assert sorted(p["id"] for p in seen["people"]) == ["li:a", "li:b", "li:c"]
    assert all(p["topics"] for p in seen["people"])
    await graph.g().delete()


def test_web_results_skip_data_brokers_and_redirects():
    from wingman import enrich
    assert not enrich._allowed({"title": "Jane Doe Email & Phone Number | Acme", "link": "https://rocketreach.co/x"})
    assert not enrich._allowed({"title": "Jane Doe Email & Phone Number", "link": "https://www.google.com/goto?url=x"})
    assert not enrich._allowed({"title": "Jane Doe(36) Portland, OR", "link": "https://example.org/x"})
    assert enrich._allowed({"title": "Acme expands its battery program", "link": "https://news.example.com/a"})
    assert enrich._clean("Jane Doe(36) Portland (503)555-0100 jane@example.com") == "Jane Doe Portland"
    assert enrich._owns({"title": "Home", "link": "https://janedoe.com/"}, "Jane Doe") == 2
    assert enrich._owns({"title": "Jane Doe – alumni profile", "link": "https://uni.example/jane"}, "Jane Doe") == 1
    assert enrich._owns({"title": "Someone else", "link": "https://example.com/"}, "Jane Doe") == 0
