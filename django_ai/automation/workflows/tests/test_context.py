from django.test import TestCase
from typing import Optional, List

from django_ai.automation.workflows import DjangoSafeBaseModel
from django_ai.utils.json import safe_model_dump
from tests.models import Guest


class TestDjangoSafeBaseModel(TestCase):
    """Tests for DjangoSafeBaseModel serialization and hydration."""

    def test_serialize_single_model(self):
        """Django model is serialized to its pk."""
        guest = Guest.objects.create(email="test@example.com", name="Test Guest")

        class Context(DjangoSafeBaseModel):
            guest: Optional[Guest] = None

        ctx = Context(guest=guest)
        data = safe_model_dump(ctx)

        self.assertEqual(data["guest"], guest.pk)

    def test_hydrate_single_model(self):
        """pk is hydrated back to Django model instance."""
        guest = Guest.objects.create(email="test@example.com", name="Test Guest")

        class Context(DjangoSafeBaseModel):
            guest: Optional[Guest] = None

        ctx = Context(guest=guest.pk)

        self.assertIsInstance(ctx.guest, Guest)
        self.assertEqual(ctx.guest.pk, guest.pk)
        self.assertEqual(ctx.guest.name, "Test Guest")

    def test_serialize_list_of_models(self):
        """List of Django models is serialized to list of pks."""
        guest1 = Guest.objects.create(email="a@test.com", name="Guest 1")
        guest2 = Guest.objects.create(email="b@test.com", name="Guest 2")

        class Context(DjangoSafeBaseModel):
            guests: List[Guest] = []

        ctx = Context(guests=[guest1, guest2])
        data = safe_model_dump(ctx)

        self.assertEqual(data["guests"], [guest1.pk, guest2.pk])

    def test_hydrate_list_of_models(self):
        """List of pks is hydrated back to list of Django model instances."""
        guest1 = Guest.objects.create(email="a@test.com", name="Guest 1")
        guest2 = Guest.objects.create(email="b@test.com", name="Guest 2")

        class Context(DjangoSafeBaseModel):
            guests: List[Guest] = []

        ctx = Context(guests=[guest1.pk, guest2.pk])

        self.assertEqual(len(ctx.guests), 2)
        self.assertIsInstance(ctx.guests[0], Guest)
        self.assertIsInstance(ctx.guests[1], Guest)
        self.assertEqual(ctx.guests[0].name, "Guest 1")
        self.assertEqual(ctx.guests[1].name, "Guest 2")

    def test_roundtrip(self):
        """Full roundtrip: model -> serialize -> hydrate -> model."""
        guest = Guest.objects.create(email="test@example.com", name="Test Guest")

        class Context(DjangoSafeBaseModel):
            guest: Optional[Guest] = None
            count: int = 0

        # Create context with model
        ctx1 = Context(guest=guest, count=5)

        # Serialize
        data = safe_model_dump(ctx1)

        # Hydrate into new context
        ctx2 = Context(**data)

        self.assertIsInstance(ctx2.guest, Guest)
        self.assertEqual(ctx2.guest.pk, guest.pk)
        self.assertEqual(ctx2.count, 5)

    def test_none_value(self):
        """None values are handled correctly."""
        class Context(DjangoSafeBaseModel):
            guest: Optional[Guest] = None

        ctx = Context(guest=None)
        data = safe_model_dump(ctx)

        self.assertIsNone(data["guest"])

        ctx2 = Context(**data)
        self.assertIsNone(ctx2.guest)

    def test_mixed_fields(self):
        """Context with both Django models and regular fields works."""
        guest = Guest.objects.create(email="test@example.com", name="Test Guest")

        class Context(DjangoSafeBaseModel):
            guest: Optional[Guest] = None
            name: str = ""
            count: int = 0
            tags: List[str] = []

        ctx = Context(guest=guest, name="test", count=10, tags=["a", "b"])
        data = safe_model_dump(ctx)

        self.assertEqual(data["guest"], guest.pk)
        self.assertEqual(data["name"], "test")
        self.assertEqual(data["count"], 10)
        self.assertEqual(data["tags"], ["a", "b"])

        ctx2 = Context(**data)
        self.assertEqual(ctx2.guest.pk, guest.pk)
        self.assertEqual(ctx2.name, "test")

    def test_already_hydrated_model_passed(self):
        """Passing an already hydrated model instance works."""
        guest = Guest.objects.create(email="test@example.com", name="Test Guest")

        class Context(DjangoSafeBaseModel):
            guest: Optional[Guest] = None

        # Pass the actual model, not pk
        ctx = Context(guest=guest)

        self.assertIsInstance(ctx.guest, Guest)
        self.assertEqual(ctx.guest.pk, guest.pk)
