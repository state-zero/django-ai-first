from django.test import TestCase
from pydantic import BaseModel, ConfigDict

from tests.models import Guest
from ..types import Django


class TestDjangoType(TestCase):
    """Test Django[Model] type wrapper for Pydantic"""

    def setUp(self):
        self.guest = Guest.objects.create(
            email="test@example.com",
            name="Test Guest",
            vip_status=False
        )

    def test_serialize_model_to_pk(self):
        """Test that Django model instances serialize to their pk"""

        class MyContext(BaseModel):
            model_config = ConfigDict(arbitrary_types_allowed=True)
            actor: Django[Guest]

        ctx = MyContext(actor=self.guest)
        data = ctx.model_dump(mode="json")

        self.assertEqual(data["actor"], self.guest.pk)

    def test_hydrate_pk_to_model(self):
        """Test that pk values hydrate back to model instances"""

        class MyContext(BaseModel):
            model_config = ConfigDict(arbitrary_types_allowed=True)
            actor: Django[Guest]

        ctx = MyContext.model_validate({"actor": self.guest.pk})

        self.assertIsInstance(ctx.actor, Guest)
        self.assertEqual(ctx.actor.pk, self.guest.pk)
        self.assertEqual(ctx.actor.email, "test@example.com")

    def test_hydrate_dict_with_pk(self):
        """Test that {'pk': value} also hydrates correctly"""

        class MyContext(BaseModel):
            model_config = ConfigDict(arbitrary_types_allowed=True)
            actor: Django[Guest]

        ctx = MyContext.model_validate({"actor": {"pk": self.guest.pk}})

        self.assertIsInstance(ctx.actor, Guest)
        self.assertEqual(ctx.actor.pk, self.guest.pk)

    def test_optional_field_none(self):
        """Test that optional Django fields handle None correctly"""

        class MyContext(BaseModel):
            model_config = ConfigDict(arbitrary_types_allowed=True)
            actor: Django[Guest] | None = None

        ctx = MyContext()
        data = ctx.model_dump(mode="json")

        self.assertIsNone(data["actor"])

    def test_optional_field_with_value(self):
        """Test that optional Django fields serialize correctly when set"""

        class MyContext(BaseModel):
            model_config = ConfigDict(arbitrary_types_allowed=True)
            actor: Django[Guest] | None = None

        ctx = MyContext(actor=self.guest)
        data = ctx.model_dump(mode="json")

        self.assertEqual(data["actor"], self.guest.pk)

    def test_hydrate_none_to_none(self):
        """Test that None deserializes to None"""

        class MyContext(BaseModel):
            model_config = ConfigDict(arbitrary_types_allowed=True)
            actor: Django[Guest] | None = None

        ctx = MyContext.model_validate({"actor": None})

        self.assertIsNone(ctx.actor)

    def test_pass_through_model_instance(self):
        """Test that passing a model instance directly works"""

        class MyContext(BaseModel):
            model_config = ConfigDict(arbitrary_types_allowed=True)
            actor: Django[Guest]

        # Hydrate should pass through if already a model instance
        ctx = MyContext(actor=self.guest)

        self.assertIsInstance(ctx.actor, Guest)
        self.assertEqual(ctx.actor.pk, self.guest.pk)

    def test_roundtrip_serialization(self):
        """Test full roundtrip: model -> JSON -> model"""

        class MyContext(BaseModel):
            model_config = ConfigDict(arbitrary_types_allowed=True)
            actor: Django[Guest]
            maybe_actor: Django[Guest] | None = None

        # Create with model instances
        ctx1 = MyContext(actor=self.guest, maybe_actor=self.guest)

        # Serialize to JSON
        json_data = ctx1.model_dump(mode="json")
        self.assertEqual(json_data, {
            "actor": self.guest.pk,
            "maybe_actor": self.guest.pk
        })

        # Deserialize back
        ctx2 = MyContext.model_validate(json_data)
        self.assertIsInstance(ctx2.actor, Guest)
        self.assertIsInstance(ctx2.maybe_actor, Guest)
        self.assertEqual(ctx2.actor.pk, self.guest.pk)
        self.assertEqual(ctx2.maybe_actor.pk, self.guest.pk)

    def test_invalid_model_class_raises_typeerror(self):
        """Test that Django[...] with non-model class raises TypeError"""

        with self.assertRaises(TypeError) as cm:
            class BadContext(BaseModel):
                actor: Django[str]

        self.assertIn("expects a Django model class", str(cm.exception))

    def test_nonexistent_pk_raises_does_not_exist(self):
        """Test that hydrating with nonexistent pk raises DoesNotExist"""

        class MyContext(BaseModel):
            model_config = ConfigDict(arbitrary_types_allowed=True)
            actor: Django[Guest]

        with self.assertRaises(Guest.DoesNotExist):
            MyContext.model_validate({"actor": 999999})

    def test_multiple_django_fields(self):
        """Test context with multiple Django model fields"""
        guest2 = Guest.objects.create(
            email="guest2@example.com",
            name="Guest Two",
            vip_status=True
        )

        class MyContext(BaseModel):
            model_config = ConfigDict(arbitrary_types_allowed=True)
            primary: Django[Guest]
            secondary: Django[Guest]

        ctx = MyContext(primary=self.guest, secondary=guest2)
        data = ctx.model_dump(mode="json")

        self.assertEqual(data["primary"], self.guest.pk)
        self.assertEqual(data["secondary"], guest2.pk)

        # Roundtrip
        ctx2 = MyContext.model_validate(data)
        self.assertEqual(ctx2.primary.name, "Test Guest")
        self.assertEqual(ctx2.secondary.name, "Guest Two")
