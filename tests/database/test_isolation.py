"""Database-level multi-tenancy isolation tests.

Builds two independent tenants (Garage A / Employee A / Customer A / Vehicle A and
Garage B / Employee B / Customer B / Vehicle B) and verifies that every
relationship correctly associates records with their own garage, with no
cross-tenant leakage at the ORM/DB level.
"""

from app.models.customer import Customer
from app.models.mot_record import MOTRecord
from app.models.vehicle import Vehicle


def test_two_garages_have_independent_users(garage, user, second_garage, second_user):
    assert user.garage_id == garage.id
    assert second_user.garage_id == second_garage.id
    assert user.garage_id != second_user.garage_id


def test_two_garages_have_independent_customers(
    garage, customer, second_garage, second_customer
):
    assert customer.garage_id == garage.id
    assert second_customer.garage_id == second_garage.id
    assert customer.garage_id != second_customer.garage_id


def test_two_garages_have_independent_vehicles(garage, vehicle, second_garage, second_vehicle):
    assert vehicle.garage_id == garage.id
    assert second_vehicle.garage_id == second_garage.id
    assert vehicle.garage_id != second_vehicle.garage_id


def test_two_garages_have_independent_mot_records(
    garage, mot_record, second_garage, second_mot_record
):
    assert mot_record.garage_id == garage.id
    assert second_mot_record.garage_id == second_garage.id
    assert mot_record.garage_id != second_mot_record.garage_id


def test_full_tenant_tree_is_isolated(
    session,
    garage,
    user,
    customer,
    vehicle,
    mot_record,
    second_garage,
    second_user,
    second_customer,
    second_vehicle,
    second_mot_record,
):
    # Garage A's tree only contains Garage A's records.
    assert {e.id for e in garage.employees} == {user.id}
    assert {c.id for c in garage.customers} == {customer.id}
    assert {v.id for v in garage.vehicles} == {vehicle.id}
    assert {r.id for r in garage.mot_records} == {mot_record.id}

    # Garage B's tree only contains Garage B's records.
    assert {e.id for e in second_garage.employees} == {second_user.id}
    assert {c.id for c in second_garage.customers} == {second_customer.id}
    assert {v.id for v in second_garage.vehicles} == {second_vehicle.id}
    assert {r.id for r in second_garage.mot_records} == {second_mot_record.id}


def test_querying_by_garage_id_never_returns_the_other_tenants_rows(
    session, garage, customer, vehicle, mot_record,
    second_garage, second_customer, second_vehicle, second_mot_record,
):
    assert Customer.query.filter_by(garage_id=garage.id).all() == [customer]
    assert Vehicle.query.filter_by(garage_id=garage.id).all() == [vehicle]
    assert MOTRecord.query.filter_by(garage_id=garage.id).all() == [mot_record]

    assert Customer.query.filter_by(garage_id=second_garage.id).all() == [second_customer]
    assert Vehicle.query.filter_by(garage_id=second_garage.id).all() == [second_vehicle]
    assert MOTRecord.query.filter_by(garage_id=second_garage.id).all() == [second_mot_record]


def test_deleting_one_garage_does_not_affect_the_other(
    session, garage, customer, vehicle, mot_record, second_garage, second_customer,
):
    session.delete(garage)
    session.commit()

    assert session.get(Customer, customer.id) is None
    assert session.get(Vehicle, vehicle.id) is None
    assert session.get(MOTRecord, mot_record.id) is None

    # Garage B's data is untouched.
    assert session.get(Customer, second_customer.id) is not None
