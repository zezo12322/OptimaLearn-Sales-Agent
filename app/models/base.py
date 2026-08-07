"""Declarative base and the one dimension every embedding in here shares."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


#: ``text-embedding-3-small`` output width. Changing this is a migration, not a
#: config change: the column type and the HNSW index both encode it.
EMBEDDING_DIM = 1536
