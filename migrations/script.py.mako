"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Политика миграций проекта (migrations/README.md): additive-first — новые
таблицы/колонки/индексы; разрушающие операции (drop_table/drop_column/
изменение типа) — только после ручного ревью и с явным бэкапом БД.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# Идентификаторы ревизии, используются Alembic.
revision: str = ${repr(up_revision)}
down_revision: Union[str, Sequence[str], None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    """Подъём схемы (additive-операции; проверяй существование объектов guard-ами)."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Откат схемы (осторожно: на прод-БД с данными выполнять только осознанно)."""
    ${downgrades if downgrades else "pass"}
