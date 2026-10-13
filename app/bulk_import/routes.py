from flask.views import MethodView
from flask_jwt_extended import jwt_required
from flask_smorest import Blueprint

from .http import ImportFormatsSchema, import_formats

bulk_import_blp = Blueprint(
    "bulk-import",
    "bulk-import",
    url_prefix="/api/bulk-import",
    description="Formats for importing services and appointments from pasted JSON. "
    "The imports themselves are POST /api/appointment-types/bulk and "
    "POST /api/appointments/bulk.",
)


@bulk_import_blp.route("/formats")
class BulkImportFormats(MethodView):
    @jwt_required()
    @bulk_import_blp.doc(security=[{"bearerAuth": []}])
    @bulk_import_blp.response(200, ImportFormatsSchema)
    def get(self):
        """The example, JSON Schema and AI prompt template for each import.

        Prompt placeholders - ``{{service_names}}``, ``{{timezone}}``,
        ``{{statuses}}`` - are for the client to fill from what it has loaded.
        """
        return import_formats()
