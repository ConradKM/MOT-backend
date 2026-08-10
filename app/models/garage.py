from datetime import datetime, timezone

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db


class Garage(db.Model):
    __tablename__ = "garages"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    users = relationship("User", back_populates="garage")
    customers = relationship("Customer", back_populates="garage")
    appointments = relationship("Appointment", back_populates="garage")
