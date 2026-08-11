from flask_smorest import Blueprint

health_blp = Blueprint(
    "health",
    "health",
    url_prefix="/api/health",
    description="Health check endpoints",
)


@health_blp.route("/")
def health():
    return {"status": "ok"}