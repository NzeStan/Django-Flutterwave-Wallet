import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name=_('Created at'))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_('Updated at'))

    class Meta:
        abstract = True


class BaseModel(TimestampedModel):
    """UUID primary key (unguessable, safe in URLs) + timestamps."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name=_('ID'))

    class Meta:
        abstract = True
