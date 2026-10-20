from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint, abort

from app.auth.decorators import owner_required
from app.auth.utils import get_current_employee
from app.models.customer import Customer

from . import service
from .schemas import (
    LoyaltyAdjustSchema,
    LoyaltyLedgerEntrySchema,
    LoyaltyProgramSchema,
    LoyaltyProgramUpdateSchema,
    LoyaltyProgressSchema,
    LoyaltyRewardSchema,
)

loyalty_blp = Blueprint(
    "loyalty",
    "loyalty",
    url_prefix="/api/loyalty",
    description="Customer loyalty / rewards programme",
)


def _get_customer_or_404(garage_id, customer_id):
    customer = Customer.query.filter_by(id=customer_id, garage_id=garage_id).first()
    if customer is None:
        abort(404, message="Customer not found")
    return customer


def _progress_payload(progress) -> dict:
    reward_available = bool(progress.available_rewards)
    return {
        "enabled": progress.program.enabled,
        "program_name": progress.program.name,
        "description": progress.program.description,
        "current_units": progress.current_units,
        "target": progress.target,
        "remaining": progress.remaining,
        "lifetime_units": progress.lifetime_units,
        "reward_available": reward_available,
        "reward_type": progress.program.reward_type,
        "reward_value_minor": progress.program.reward_value_minor,
        "currency": progress.program.currency,
        "available_rewards": progress.available_rewards,
    }


@loyalty_blp.route("/program")
class ProgramResource(MethodView):
    @jwt_required()
    @loyalty_blp.response(200, LoyaltyProgramSchema)
    def get(self):
        garage_id = get_current_employee().garage_id
        return service.get_or_create_program(garage_id)

    @jwt_required()
    @owner_required
    @loyalty_blp.arguments(LoyaltyProgramUpdateSchema)
    @loyalty_blp.response(200, LoyaltyProgramSchema)
    def patch(self, data):
        garage_id = get_current_employee().garage_id
        program = service.get_or_create_program(garage_id)
        try:
            return service.update_program(program, data)
        except service.LoyaltyError as exc:
            abort(exc.code, message=exc.message)


@loyalty_blp.route("/customers/<uuid:customer_id>/progress")
class CustomerProgressResource(MethodView):
    @jwt_required()
    @loyalty_blp.response(200, LoyaltyProgressSchema)
    def get(self, customer_id):
        employee = get_current_employee()
        _get_customer_or_404(employee.garage_id, customer_id)
        progress = service.get_customer_progress(employee.garage_id, customer_id)
        if progress is None:
            abort(404, message="Loyalty is not configured for this business.")
        return _progress_payload(progress)


@loyalty_blp.route("/customers/<uuid:customer_id>/history")
class CustomerHistoryResource(MethodView):
    @jwt_required()
    @loyalty_blp.response(200, LoyaltyLedgerEntrySchema(many=True))
    def get(self, customer_id):
        employee = get_current_employee()
        _get_customer_or_404(employee.garage_id, customer_id)
        return service.get_customer_history(employee.garage_id, customer_id)


@loyalty_blp.route("/customers/<uuid:customer_id>/adjust")
class CustomerAdjustResource(MethodView):
    @jwt_required()
    @owner_required
    @loyalty_blp.arguments(LoyaltyAdjustSchema)
    @loyalty_blp.response(200, LoyaltyLedgerEntrySchema)
    def post(self, data, customer_id):
        employee = get_current_employee()
        customer = _get_customer_or_404(employee.garage_id, customer_id)
        try:
            return service.adjust(
                garage=employee.garage,
                customer=customer,
                actor=employee,
                delta=data["delta"],
                reason=data["reason"],
            )
        except service.LoyaltyError as exc:
            abort(exc.code, message=exc.message)


@loyalty_blp.route("/customers/<uuid:customer_id>/rewards/<uuid:reward_id>/redeem")
class RewardRedeemResource(MethodView):
    @jwt_required()
    @loyalty_blp.response(200, LoyaltyRewardSchema)
    def post(self, customer_id, reward_id):
        employee = get_current_employee()
        customer = _get_customer_or_404(employee.garage_id, customer_id)
        try:
            return service.redeem_reward(
                garage=employee.garage, customer=customer, reward_id=reward_id, actor=employee
            )
        except service.LoyaltyError as exc:
            abort(exc.code, message=exc.message)
