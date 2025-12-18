from django.test import TestCase
from pydantic import BaseModel
from typing import List, Any

from django_ai.utils.json import safe_model_dump, restore_model_data
from tests.models import Guest


class TestSafeModelDump(TestCase):
    """Test safe_model_dump and restore_model_data with Django model instances"""

    def test_single_django_model(self):
        """Test serializing and restoring a single Django model instance."""

        class Context(BaseModel):
            model_config = {"arbitrary_types_allowed": True}
            guest: Any = None

        guest = Guest(pk=1, email="test@example.com", name="Test Guest")
        ctx = Context(guest=guest)

        # Serialize
        data = safe_model_dump(ctx)
        self.assertIsInstance(data, dict)

        # Restore
        restored = restore_model_data(data)
        self.assertEqual(restored["guest"].pk, 1)
        self.assertEqual(restored["guest"].name, "Test Guest")
        self.assertEqual(restored["guest"].email, "test@example.com")

    def test_list_of_django_models(self):
        """Test serializing and restoring a list of Django models."""

        class Context(BaseModel):
            model_config = {"arbitrary_types_allowed": True}
            guests: List[Any] = []

        guest1 = Guest(pk=1, email="a@test.com", name="Guest 1")
        guest2 = Guest(pk=2, email="b@test.com", name="Guest 2")
        ctx = Context(guests=[guest1, guest2])

        data = safe_model_dump(ctx)
        restored = restore_model_data(data)

        self.assertEqual(len(restored["guests"]), 2)
        self.assertEqual(restored["guests"][0].pk, 1)
        self.assertEqual(restored["guests"][1].pk, 2)

    def test_nested_django_model(self):
        """Test serializing and restoring nested Django models."""

        class Context(BaseModel):
            model_config = {"arbitrary_types_allowed": True}
            data: dict = {}

        guest = Guest(pk=1, email="test@example.com", name="Test Guest")
        ctx = Context(data={"nested": {"guest": guest}})

        data = safe_model_dump(ctx)
        restored = restore_model_data(data)

        self.assertEqual(restored["data"]["nested"]["guest"].pk, 1)
