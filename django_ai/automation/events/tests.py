from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from datetime import datetime, timedelta
from ..events.models import Event, EventStatus
from ..events.definitions import EventDefinition
from tests.models import TestBooking, TestOrder, TestProperty, TestModelWithoutEvents

class EventDefinitionTest(TestCase):
    """Test the EventDefinition helper class functionality"""

    def test_event_definition_creation_with_date_field(self):
        """Test EventDefinition creation for scheduled events"""
        event_def = EventDefinition(
            "test_event",
            date_field="checkin_date",
            condition=lambda i: i.status == "confirmed",
        )

        self.assertEqual(event_def.name, "test_event")
        self.assertEqual(event_def.date_field, "checkin_date")
        self.assertFalse(event_def.is_immediate())

        # Test condition evaluation
        booking = TestBooking(status="confirmed")
        self.assertTrue(event_def.should_create(booking))

        booking.status = "pending"
        self.assertFalse(event_def.should_create(booking))

    def test_event_definition_immediate_event(self):
        """Test EventDefinition creation for immediate events"""
        event_def = EventDefinition(
            "immediate_event", condition=lambda i: i.status == "confirmed"
        )

        self.assertEqual(event_def.name, "immediate_event")
        self.assertIsNone(event_def.date_field)
        self.assertTrue(event_def.is_immediate())

    def test_event_definition_no_condition(self):
        """Test EventDefinition with no condition (always True)"""
        event_def = EventDefinition("always_event", date_field="checkin_date")

        booking = TestBooking(status="anything")
        self.assertTrue(event_def.should_create(booking))

    def test_get_date_value_scheduled(self):
        """Test extracting date value from model instance for scheduled events"""
        event_def = EventDefinition("test_event", date_field="checkin_date")
        test_date = timezone.now()
        booking = TestBooking(checkin_date=test_date)

        self.assertEqual(event_def.get_date_value(booking), test_date)

    def test_get_date_value_immediate(self):
        """Test date value for immediate events returns None"""
        event_def = EventDefinition("immediate_event")
        booking = TestBooking()

        self.assertIsNone(event_def.get_date_value(booking))


class EventModelTest(TransactionTestCase):
    """Test the Event model functionality and properties"""

    def setUp(self):
        """Create test models for use in event tests"""
        self.test_date = timezone.now() + timedelta(days=1)
        self.booking = TestBooking.objects.create(
            guest_name="John Doe",
            checkin_date=self.test_date,
            checkout_date=self.test_date + timedelta(days=2),
            status="confirmed",
        )

        self.order = TestOrder.objects.create(total=1500.00, status="confirmed")

    def test_event_creation_on_save(self):
        """Test that events are automatically created when model is saved"""
        # Test booking events
        booking_events = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(self.booking.id),
        )

        # Should have created 5 events (4 scheduled + 1 immediate)
        self.assertEqual(booking_events.count(), 5)

        event_names = set(booking_events.values_list("event_name", flat=True))
        expected_names = {
            "checkin_due",
            "checkout_due",
            "review_request",
            "always_event",
            "booking_confirmed",
        }
        self.assertEqual(event_names, expected_names)

        # Test order events
        order_events = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestOrder),
            entity_id=str(self.order.id),
        )

        # Should have created 2 immediate events
        self.assertEqual(order_events.count(), 2)

        order_event_names = set(order_events.values_list("event_name", flat=True))
        expected_order_names = {"order_placed", "high_value_alert"}
        self.assertEqual(order_event_names, expected_order_names)

    def test_event_entity_property(self):
        """Test that event.entity returns the correct model instance"""
        event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(self.booking.id),
            event_name="checkin_due",
        ).first()

        entity = event.entity
        self.assertEqual(entity.id, self.booking.id)
        self.assertEqual(entity.guest_name, self.booking.guest_name)

    def test_event_at_property_scheduled(self):
        """Test that event.at returns the correct date for scheduled events"""
        event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(self.booking.id),
            event_name="checkin_due",
        ).first()

        # event.at should pull from the entity's checkin_date field
        self.assertEqual(event.at, self.booking.checkin_date)
        self.assertFalse(event.is_immediate)

    def test_event_at_property_immediate(self):
        """Test that event.at returns creation time for immediate events"""
        event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestOrder),
            entity_id=str(self.order.id),
            event_name="order_placed",
        ).first()

        # event.at should be the creation time for immediate events
        self.assertEqual(event.at, event.event_created_at)
        self.assertTrue(event.is_immediate)

    def test_event_is_valid_property(self):
        """Test that event.is_valid correctly evaluates conditions at runtime"""
        # Get events for confirmed booking
        checkin_event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(self.booking.id),
            event_name="checkin_due",
        ).first()

        review_event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(self.booking.id),
            event_name="review_request",
        ).first()

        booking_confirmed_event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(self.booking.id),
            event_name="booking_confirmed",
        ).first()

        # checkin_due should be valid (status='confirmed' meets condition)
        self.assertTrue(checkin_event.is_valid)

        # review_request should be invalid (status needs to be 'completed')
        self.assertFalse(review_event.is_valid)

        # booking_confirmed (immediate) should be valid (status='confirmed')
        self.assertTrue(booking_confirmed_event.is_valid)

    def test_event_validity_changes_with_status(self):
        """Test that event validity changes dynamically when model status changes"""
        review_event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(self.booking.id),
            event_name="review_request",
        ).first()

        # Initially invalid (status='confirmed', needs 'completed')
        self.assertFalse(review_event.is_valid)

        # Change status to completed
        self.booking.status = "completed"
        self.booking.save()

        # Now should be valid (condition now met)
        self.assertTrue(review_event.is_valid)

    def test_event_deletion_on_model_delete(self):
        """Test that events are automatically deleted when model is deleted"""
        booking_id = self.booking.id

        # Verify events exist before deletion
        events_count = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(booking_id),
        ).count()
        self.assertGreater(events_count, 0)

        # Delete the booking
        self.booking.delete()

        # Verify all events are deleted via signal
        events_count = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(booking_id),
        ).count()
        self.assertEqual(events_count, 0)

    def test_no_events_for_model_without_events_attribute(self):
        """Test that no events are created for models without events attribute"""
        model_without_events = TestModelWithoutEvents.objects.create(name="Test")

        events_count = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestModelWithoutEvents),
            entity_id=str(model_without_events.id),
        ).count()

        # Should be 0 since TestModelWithoutEvents has no 'events' attribute
        self.assertEqual(events_count, 0)


class EventManagerTest(TransactionTestCase):
    """Test the EventManager query methods"""

    def setUp(self):
        """Create test models with different dates and statuses"""
        self.now = timezone.now()
        self.today = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.tomorrow = self.today + timedelta(days=1)
        self.next_week = self.today + timedelta(days=7)

        # Create bookings with different dates and statuses
        self.booking_today = TestBooking.objects.create(
            guest_name="Today Guest",
            checkin_date=self.today + timedelta(hours=12),
            checkout_date=self.today + timedelta(days=1),
            status="confirmed",
        )

        self.booking_tomorrow = TestBooking.objects.create(
            guest_name="Tomorrow Guest",
            checkin_date=self.tomorrow + timedelta(hours=12),
            checkout_date=self.tomorrow + timedelta(days=1),
            status="confirmed",
        )

        self.booking_cancelled = TestBooking.objects.create(
            guest_name="Cancelled Guest",
            checkin_date=self.today + timedelta(hours=12),
            checkout_date=self.today + timedelta(days=1),
            status="cancelled",  # This makes conditional events invalid
        )

        # Create orders for immediate event testing
        self.order_high_value = TestOrder.objects.create(
            total=2000.00, status="confirmed"
        )

        self.order_low_value = TestOrder.objects.create(total=50.00, status="pending")

    def test_get_events_date_range(self):
        """Test getting events within a specific date range"""
        events = Event.objects.get_events(
            from_date=self.today,
            to_date=self.tomorrow + timedelta(hours=23, minutes=59),
        )

        # Expected scheduled events within range:
        # - Today Guest: checkin_due, checkout_due, always_event (3 valid)
        # - Tomorrow Guest: checkin_due, always_event (2 valid, checkout_due is day after tomorrow)
        # - Cancelled Guest: always_event only (1 valid, conditional events invalid)
        # Total scheduled events: 6
        # Plus immediate events that are included by default
        self.assertGreaterEqual(len(events), 6)

    def test_get_events_scheduled_only(self):
        """Test getting only scheduled events in date range"""
        scheduled_events = Event.objects.get_due_events(
            from_date=self.today,
            to_date=self.tomorrow + timedelta(hours=23, minutes=59),
        )

        # Should not include immediate events
        for event in scheduled_events:
            self.assertFalse(event.is_immediate)
            self.assertIsNotNone(event.at)

    def test_get_events_with_status_filter(self):
        # Baseline counts before we flip one scheduled event
        baseline_pending = len(Event.objects.get_events(
            from_date=self.today,
            to_date=self.next_week + timedelta(days=1),
            status=EventStatus.PENDING,
        ))
        baseline_processed = len(Event.objects.get_events(
            from_date=self.today,
            to_date=self.next_week + timedelta(days=1),
            status=EventStatus.PROCESSED,
        ))

        # Flip a scheduled (non-immediate) event to processed
        event = Event.objects.filter(
            event_name="checkin_due",
            status=EventStatus.PENDING,
        ).first()
        event.status = EventStatus.PROCESSED
        event.save()

        pending_events = Event.objects.get_events(
            from_date=self.today,
            to_date=self.next_week + timedelta(days=1),
            status=EventStatus.PENDING,
        )
        processed_events = Event.objects.get_events(
            from_date=self.today,
            to_date=self.next_week + timedelta(days=1),
            status=EventStatus.PROCESSED,
        )

        # Verify the filter works: one fewer pending, one more processed
        self.assertEqual(len(pending_events), baseline_pending - 1)
        self.assertEqual(len(processed_events), baseline_processed + 1)

    def test_get_events_only_returns_valid_events(self):
        """Test that get_events only returns events that are currently valid"""
        events = Event.objects.get_events(
            from_date=self.today,
            to_date=self.tomorrow + timedelta(hours=23, minutes=59),
        )

        # All returned events should pass their validity conditions
        for event in events:
            self.assertTrue(event.is_valid, f"Event {event.event_name} should be valid")

    def test_get_events_empty_range(self):
        """Test getting events with a date range that contains no events"""
        future_date = self.now + timedelta(days=365)
        events = Event.objects.get_due_events(
            from_date=future_date, to_date=future_date + timedelta(days=1)
        )

        # Should return no events for far future date range
        self.assertEqual(len(events), 0)

    def test_get_events_no_date_range(self):
        """Test getting all events without date range"""
        all_events = Event.objects.get_events()

        # Should include both immediate and scheduled events
        immediate_events = [e for e in all_events if e.is_immediate]
        scheduled_events = [e for e in all_events if not e.is_immediate]

        self.assertGreater(len(immediate_events), 0)
        self.assertGreater(len(scheduled_events), 0)

    def test_update_for_instance(self):
        """Test manual event creation for a model instance"""
        property_instance = TestProperty.objects.create(
            name="Test Property", maintenance_due=self.tomorrow, status="active"
        )

        # Verify event was automatically created via signal
        events = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestProperty),
            entity_id=str(property_instance.id),
        )
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().event_name, "maintenance_due")


class EventStringRepresentationTest(TransactionTestCase):
    """Test string representation and display of events"""

    def test_event_str_method_scheduled(self):
        """Test the string representation of scheduled Event objects"""
        booking = TestBooking.objects.create(
            guest_name="Test Guest",
            checkin_date=timezone.now(),
            checkout_date=timezone.now() + timedelta(days=1),
            status="confirmed",
        )

        event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(booking.id),
            event_name="checkin_due",
        ).first()

        str_repr = str(event)
        # Should contain event name, time, and model info
        self.assertIn("checkin_due", str_repr)
        self.assertIn("testbooking", str_repr.lower())
        self.assertIn(str(booking.id), str_repr)

    def test_event_str_method_immediate(self):
        """Test the string representation of immediate Event objects"""
        order = TestOrder.objects.create(total=1500.00, status="confirmed")

        event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestOrder),
            entity_id=str(order.id),
            event_name="order_placed",
        ).first()

        str_repr = str(event)
        # Should contain event name and creation time
        self.assertIn("order_placed", str_repr)
        self.assertIn("testorder", str_repr.lower())


class EventErrorHandlingTest(TransactionTestCase):
    """Test error handling and edge cases"""

    def test_event_with_deleted_entity(self):
        """Test event behavior when referenced entity no longer exists"""
        booking = TestBooking.objects.create(
            guest_name="Test Guest",
            checkin_date=timezone.now(),
            checkout_date=timezone.now() + timedelta(days=1),
            status="confirmed",
        )

        event = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(booking.id),
            event_name="checkin_due",
        ).first()

        # Delete the booking (this should also delete events via signal)
        booking.delete()

        # Create an orphaned event to test graceful error handling
        orphaned_event = Event.objects.create(
            event_name="orphaned_event",
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id="999999",  # Non-existent ID
        )

        # These should not raise exceptions but return None/False gracefully
        self.assertIsNone(orphaned_event.at)
        self.assertFalse(orphaned_event.is_valid)
        self.assertIsNone(orphaned_event._get_event_definition())

    def test_immediate_event_conditions(self):
        """Test that immediate events respect their conditions"""
        # Order with low value should not trigger high_value_alert
        low_value_order = TestOrder.objects.create(total=50.00, status="confirmed")

        events = Event.objects.filter(
            model_type=ContentType.objects.get_for_model(TestOrder),
            entity_id=str(low_value_order.id),
        )

        # Should have order_placed but not high_value_alert
        event_names = set(events.values_list("event_name", flat=True))
        self.assertIn("order_placed", event_names)

        # Check validity
        for event in events:
            if event.event_name == "order_placed":
                self.assertTrue(event.is_valid)
            elif event.event_name == "high_value_alert":
                self.assertFalse(
                    event.is_valid
                )  # Total is only 50, condition is > 1000


class EventNamespaceTest(TransactionTestCase):
    """Test namespace functionality for events and callbacks"""

    def setUp(self):
        from django_ai.automation.events.callbacks import callback_registry

        callback_registry.clear()

    def tearDown(self):
        from django_ai.automation.events.callbacks import callback_registry

        callback_registry.clear()

    def test_event_creation_with_custom_namespace(self):
        """Test that events can be created with custom namespaces"""
        # Create a booking - should use default namespace "*"
        booking = TestBooking.objects.create(
            guest_name="Test Guest",
            checkin_date=timezone.now() + timedelta(days=1),
            checkout_date=timezone.now() + timedelta(days=2),
            status="confirmed",
        )

        event = Event.objects.get(
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id=str(booking.id),
            event_name="booking_confirmed",
        )

        # Default namespace should be "*"
        self.assertEqual(event.namespace, "*")

    def test_callback_namespace_filtering(self):
        """Test that callbacks are filtered by namespace"""
        from django_ai.automation.events.callbacks import callback_registry, on_event

        callback_results = []

        @on_event(event_name="booking_confirmed", namespace="tenant_a")
        def tenant_a_callback(event):
            callback_results.append(f"tenant_a_{event.id}")

        @on_event(event_name="booking_confirmed", namespace="tenant_b")
        def tenant_b_callback(event):
            callback_results.append(f"tenant_b_{event.id}")

        @on_event(event_name="booking_confirmed", namespace="*")
        def wildcard_callback(event):
            callback_results.append(f"wildcard_{event.id}")

        # Create event with tenant_a namespace
        event_a = Event.objects.create(
            event_name="booking_confirmed",
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id="123",
            namespace="tenant_a",
        )

        # Create event with tenant_b namespace
        event_b = Event.objects.create(
            event_name="booking_confirmed",
            model_type=ContentType.objects.get_for_model(TestBooking),
            entity_id="456",
            namespace="tenant_b",
        )

        # Trigger callbacks
        event_a.mark_as_occurred()
        event_b.mark_as_occurred()

        # Check results
        self.assertIn(f"tenant_a_{event_a.id}", callback_results)
        self.assertIn(
            f"wildcard_{event_a.id}", callback_results
        )  # Wildcard should match
        self.assertNotIn(f"tenant_b_{event_a.id}", callback_results)  # Wrong namespace

        self.assertIn(f"tenant_b_{event_b.id}", callback_results)
        self.assertIn(
            f"wildcard_{event_b.id}", callback_results
        )  # Wildcard should match
        self.assertNotIn(f"tenant_a_{event_b.id}", callback_results)  # Wrong namespace

    def test_wildcard_namespace_matches_all(self):
        """Test that wildcard namespace "*" matches all events"""
        from django_ai.automation.events.callbacks import callback_registry, on_event

        callback_results = []

        @on_event(event_name="booking_confirmed", namespace="*")
        def wildcard_callback(event):
            callback_results.append(f"wildcard_{event.namespace}_{event.id}")

        # Create events with different namespaces
        namespaces = ["tenant_a", "tenant_b", "custom_namespace", "*"]
        events = []

        for ns in namespaces:
            event = Event.objects.create(
                event_name="booking_confirmed",
                model_type=ContentType.objects.get_for_model(TestBooking),
                entity_id=f"test_{ns}",
                namespace=ns,
            )
            events.append(event)
            event.mark_as_occurred()

        # Wildcard callback should have been called for all events
        for event in events:
            self.assertIn(f"wildcard_{event.namespace}_{event.id}", callback_results)
