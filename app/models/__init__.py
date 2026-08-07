"""Model registry.

Importing the package registers every mapped class on ``Base.metadata``, which
is what Alembic walks. A new model module must be imported here or its tables
become invisible to migrations.
"""

from app.models import sales  # noqa: F401
from app.models.base import EMBEDDING_DIM, Base  # noqa: F401
