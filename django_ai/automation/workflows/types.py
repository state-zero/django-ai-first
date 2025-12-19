from __future__ import annotations

from typing import Any
from typing_extensions import Annotated

from pydantic.functional_serializers import PlainSerializer
from pydantic.functional_validators import BeforeValidator
from django.db import models


class Django:
    """
    Type wrapper for Django models in Pydantic contexts.

    Use as: user: Django[User]

    - Stores as: pk (int/str) in JSON when serializing
    - Hydrates eagerly: pk -> Model.objects.get(pk=pk) when deserializing

    Example:
        from pydantic import BaseModel
        from django.contrib.auth.models import User

        class MyContext(BaseModel):
            actor: Django[User]
            maybe_actor: Django[User] | None = None

        ctx = MyContext(actor=User.objects.get(pk=1))
        ctx.model_dump(mode='json')  # {"actor": 1, "maybe_actor": null}

        ctx2 = MyContext.model_validate({"actor": 1})
        assert isinstance(ctx2.actor, User)  # hydrated eagerly
    """

    def __class_getitem__(cls, model_cls: type[models.Model]):
        if not (isinstance(model_cls, type) and issubclass(model_cls, models.Model)):
            raise TypeError("Django[...] expects a Django model class")

        def _serialize(v: Any) -> Any:
            if isinstance(v, model_cls):
                return v.pk
            return v

        def _hydrate(v: Any) -> Any:
            if v is None or isinstance(v, model_cls):
                return v

            # Allow {"pk": ...} as well, if it ever shows up
            if isinstance(v, dict) and "pk" in v:
                v = v["pk"]

            # Eager DB fetch
            return model_cls.objects.get(pk=v)

        return Annotated[
            model_cls,
            PlainSerializer(_serialize, return_type=Any),
            BeforeValidator(_hydrate),
        ]
