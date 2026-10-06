"""Industrial intelligence center (v37): isolated append-only domain.

Boundaries (ADR-0005 and ``docs/industrial-intelligence-center.md``):

- Writes only ``intelligence_*`` tables created by migration v37.
- Read-only projection of existing governed sources and news records.
- Every item/event revision and daily brief is fixed to
  ``prediction_eligible=0`` and ``instruction_eligible=0`` at the database
  layer; the application cannot relax this.
- No instant business alerts: this package contains no push, streaming,
  polling, email, or callback-notification paths of any kind.
"""

from .identity import SCHEMA_VERSION  # re-export for convenience
from .schema import (
    INDUSTRIAL_INTELLIGENCE_MIGRATION_NAME,
    INDUSTRIAL_INTELLIGENCE_MIGRATION_VERSION,
)

__all__ = [
    "SCHEMA_VERSION",
    "INDUSTRIAL_INTELLIGENCE_MIGRATION_NAME",
    "INDUSTRIAL_INTELLIGENCE_MIGRATION_VERSION",
]
