from app.models.ai_voice_faq import GarageVoiceFAQ

BASE = "/api/ai-voice/faqs/"


def test_owner_can_manage_faq_lifecycle_and_reorder(authenticated_client):
    first = authenticated_client.post(
        BASE, json={"question": "Can customers wait?", "answer": "Yes"}
    )
    assert first.status_code == 201
    second = authenticated_client.post(
        BASE, json={"question": "Where to park?", "answer": "Rear car park"}
    )
    assert second.status_code == 201
    first_id = first.get_json()["id"]
    second_id = second.get_json()["id"]

    assert (
        authenticated_client.patch(f"{BASE}{first_id}", json={"is_enabled": False}).status_code
        == 200
    )
    ordered = authenticated_client.put(f"{BASE}order", json={"ids": [second_id, first_id]})
    assert ordered.status_code == 200
    assert [item["id"] for item in ordered.get_json()] == [second_id, first_id]
    assert authenticated_client.delete(f"{BASE}{first_id}").status_code == 204
    assert authenticated_client.get(f"{BASE}{first_id}").status_code == 404
    archived = GarageVoiceFAQ.query.filter_by(id=first_id).one()
    assert archived.archived_at is not None and archived.is_enabled is False


def test_faq_api_cannot_cross_tenant(authenticated_client, second_authenticated_client):
    created = authenticated_client.post(
        BASE, json={"question": "Courtesy car?", "answer": "Ask us"}
    )
    faq_id = created.get_json()["id"]
    assert second_authenticated_client.get(f"{BASE}{faq_id}").status_code == 404
    assert (
        second_authenticated_client.patch(f"{BASE}{faq_id}", json={"answer": "Leaked"}).status_code
        == 404
    )
