from typing import Any, get_origin, get_args, Union
from pydantic import BaseModel, ConfigDict, model_validator, model_serializer
from django.db.models import Model as DjangoModel


def _serialize(value: Any) -> Any:
    """Convert Django models to pk, recursively."""
    if isinstance(value, DjangoModel):
        return value.pk
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(v) for v in value]
    return value


def _get_django_model_class(type_hint):
    """Extract Django model class from a type hint."""
    if isinstance(type_hint, type) and issubclass(type_hint, DjangoModel):
        return type_hint

    origin = get_origin(type_hint)
    if origin is Union:
        for arg in get_args(type_hint):
            if isinstance(arg, type) and issubclass(arg, DjangoModel):
                return arg
    if origin is list:
        args = get_args(type_hint)
        if args:
            return _get_django_model_class(args[0])

    return None


def _hydrate_field(value: Any, type_hint) -> Any:
    """Hydrate a field value based on its type hint."""
    if value is None:
        return None

    model_class = _get_django_model_class(type_hint)

    if get_origin(type_hint) is list and isinstance(value, list):
        inner_model = _get_django_model_class(get_args(type_hint)[0]) if get_args(type_hint) else None
        if inner_model:
            return [
                item if isinstance(item, DjangoModel) else inner_model.objects.get(pk=item)
                for item in value
            ]
        return value

    if model_class and not isinstance(value, DjangoModel):
        return model_class.objects.get(pk=value)

    return value


class DjangoSafeBaseModel(BaseModel):
    """
    Base class for Pydantic models that can hold Django model instances.

    Automatically serializes Django models to their pk on dump, and hydrates
    pks back to model instances on validation based on the field's type hint.

    Example:
        class MyContext(DjangoSafeBaseModel):
            user: Optional[User] = None
            items: List[Item] = []

        # Serialization: user becomes user.pk
        data = safe_model_dump(ctx)  # {"user": 123, "items": [1, 2, 3]}

        # Hydration: pk becomes User instance
        ctx = MyContext(**data)  # ctx.user is User(pk=123)
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @model_validator(mode='before')
    @classmethod
    def _hydrate_models(cls, data: Any) -> Any:
        """Hydrate Django model pks back to instances based on type hints."""
        if not isinstance(data, dict):
            return data

        result = dict(data)
        for field_name, field_info in cls.model_fields.items():
            if field_name in result:
                result[field_name] = _hydrate_field(result[field_name], field_info.annotation)

        return result

    @model_serializer(mode='wrap')
    def _serialize_models(self, handler) -> dict:
        """Serialize Django model instances to their pks."""
        data = handler(self)
        return _serialize(data)
