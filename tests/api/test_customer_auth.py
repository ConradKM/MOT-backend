"""API tests for POST /api/customer/auth/login and /api/customer/auth/refresh.

Customer login is knowledge-factor only: an email plus a registration number of
a vehicle on that customer's account. No customer password exists.
"""


def _login(client, email, registration_number):
    return client.post(
        "/api/customer/auth/login",
        json={"email": email, "registration_number": registration_number},
    )


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------


def test_login_with_email_and_registration_succeeds(client, customer, vehicle):
    resp = _login(client, customer.email, vehicle.registration_number)

    assert resp.status_code == 200
    body = resp.get_json()
    assert body.get("access_token")
    assert body.get("refresh_token")


def test_login_normalises_registration_input(client, customer, vehicle):
    # vehicle.registration_number is stored normalised as "AB12CDE"
    resp = _login(client, customer.email, "  ab 12 cde ")

    assert resp.status_code == 200


def test_login_email_is_case_insensitive(client, customer, vehicle):
    resp = _login(client, customer.email.upper(), vehicle.registration_number)

    assert resp.status_code == 200


def test_login_wrong_email_fails(client, customer, vehicle):
    resp = _login(client, "someone-else@example.com", vehicle.registration_number)

    assert resp.status_code == 401


def test_login_wrong_registration_fails(client, customer, vehicle):
    resp = _login(client, customer.email, "ZZ99ZZZ")

    assert resp.status_code == 401


def test_login_registration_from_another_customer_fails(
    client, customer, second_customer, second_vehicle
):
    # second_vehicle belongs to second_customer (a different garage); pairing it
    # with the first customer's email must not authenticate anyone.
    resp = _login(client, customer.email, second_vehicle.registration_number)

    assert resp.status_code == 401


def test_login_missing_email_fails(client, vehicle):
    resp = client.post(
        "/api/customer/auth/login",
        json={"registration_number": vehicle.registration_number},
    )
    assert resp.status_code == 422


def test_login_missing_registration_fails(client, customer):
    resp = client.post("/api/customer/auth/login", json={"email": customer.email})
    assert resp.status_code == 422


def test_login_does_not_reveal_which_field_was_wrong(client, customer, vehicle):
    wrong_email = _login(client, "nobody@example.com", vehicle.registration_number)
    wrong_reg = _login(client, customer.email, "ZZ99ZZZ")

    assert wrong_email.get_json()["message"] == wrong_reg.get_json()["message"]


def test_login_token_is_accepted_by_the_customer_portal(client, customer, vehicle):
    token = _login(client, customer.email, vehicle.registration_number).get_json()[
        "access_token"
    ]

    resp = client.get(
        "/api/customer/account", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.get_json()["customer"]["id"] == str(customer.id)


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------


def test_refresh_with_customer_refresh_token_returns_a_new_access_token(
    client, customer, vehicle
):
    refresh_token = _login(client, customer.email, vehicle.registration_number).get_json()[
        "refresh_token"
    ]

    resp = client.post(
        "/api/customer/auth/refresh",
        headers={"Authorization": f"Bearer {refresh_token}"},
    )

    assert resp.status_code == 200
    assert resp.get_json().get("access_token")


def test_refresh_rejects_a_customer_access_token(client, customer, vehicle):
    access_token = _login(client, customer.email, vehicle.registration_number).get_json()[
        "access_token"
    ]

    resp = client.post(
        "/api/customer/auth/refresh",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert resp.status_code != 200


def test_refresh_rejects_an_employee_refresh_token(client, refresh_token):
    # refresh_token fixture is a garage employee's refresh token.
    resp = client.post(
        "/api/customer/auth/refresh",
        headers={"Authorization": f"Bearer {refresh_token}"},
    )

    assert resp.status_code == 401


def test_refresh_missing_token_fails(client):
    resp = client.post("/api/customer/auth/refresh")
    assert resp.status_code == 401
