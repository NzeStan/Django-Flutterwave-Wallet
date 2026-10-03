"""Small helpers that keep the models (and migrations) working on Django 4.2 through 6.x."""
import django
from django.db import models


def check_constraint(condition, name):
    """``CheckConstraint`` takes ``condition=`` from Django 5.1 and ``check=`` before."""
    if django.VERSION >= (5, 1):
        return models.CheckConstraint(condition=condition, name=name)
    return models.CheckConstraint(check=condition, name=name)  # pragma: no cover - Django < 5.1
