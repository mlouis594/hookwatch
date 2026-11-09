import json


def send(client, source="stripe", payload=None, **kwargs):
    payload = payload if payload is not None else {"type": "customer.created", "id": "evt_1"}
    return client.post(f"/hooks/{source}", json=payload, **kwargs)


def test_receive_webhook_stores_event(client):
    response = send(client)

    assert response.status_code == 200
    assert response.json() == {"id": 1, "received": True}


def test_event_detail_has_headers_body_and_parsed_json(client):
    send(client, headers={"Stripe-Signature": "t=1,v1=abc"})

    detail = client.get("/events/1").json()

    assert detail["source"] == "stripe"
    assert detail["method"] == "POST"
    assert detail["path"] == "/hooks/stripe"
    assert detail["headers"]["stripe-signature"] == "t=1,v1=abc"
    assert detail["json_body"] == {"type": "customer.created", "id": "evt_1"}
    assert json.loads(detail["body"]) == detail["json_body"]
    assert detail["signature_status"] == "unchecked"


def test_sensitive_headers_are_redacted(client):
    send(client, headers={"Authorization": "Bearer secret-token"})

    headers = client.get("/events/1").json()["headers"]

    assert headers["authorization"] == "[redacted]"


def test_non_json_body_is_kept_as_text(client):
    client.post(
        "/hooks/github",
        content=b"plain text payload",
        headers={"Content-Type": "text/plain"},
    )

    detail = client.get("/events/1").json()

    assert detail["body"] == "plain text payload"
    assert detail["json_body"] is None


def test_invalid_source_is_rejected(client):
    assert client.post("/hooks/Not Valid!", json={}).status_code == 422
    assert client.post("/hooks/UPPER", json={}).status_code == 422


def test_oversized_body_is_rejected(client):
    response = client.post("/hooks/stripe", content=b"x" * 1_000_001)

    assert response.status_code == 413


def test_list_is_newest_first_and_filters_by_source(client):
    send(client, source="stripe")
    send(client, source="github")
    send(client, source="stripe")

    all_ids = [e["id"] for e in client.get("/events").json()]
    stripe_ids = [e["id"] for e in client.get("/events", params={"source": "stripe"}).json()]

    assert all_ids == [3, 2, 1]
    assert stripe_ids == [3, 1]


def test_list_supports_limit_and_offset(client):
    for _ in range(5):
        send(client)

    page = client.get("/events", params={"limit": 2, "offset": 1}).json()

    assert [e["id"] for e in page] == [4, 3]


def test_missing_event_returns_404(client):
    response = client.get("/events/999")

    assert response.status_code == 404
    assert response.json()["detail"] == "Event 999 not found"
