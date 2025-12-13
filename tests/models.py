from django_ai.automation.events.models import EventDefinition, Event

from django.db import models
import uuid

from django.db import models
from django_ai.automation.events.definitions import EventDefinition, EventTrigger

class ITImmediate(models.Model):
    flag = models.BooleanField(default=False)
    events = [EventDefinition("immediate_evt", condition=lambda i: i.flag)]


class ITScheduled(models.Model):
    due_at = models.DateTimeField(null=True, blank=True)
    events = [
        EventDefinition("scheduled_evt", date_field="due_at", condition=lambda i: True)
    ]

# Test models for the events system
class TestBooking(models.Model):
    """Test model representing a booking with multiple event types"""

    guest_name = models.CharField(max_length=100)
    checkin_date = models.DateTimeField()
    checkout_date = models.DateTimeField()
    status = models.CharField(max_length=20, default="pending")

    events = [
        EventDefinition(
            "checkin_due",
            date_field="checkin_date",
            condition=lambda instance: instance.status == "confirmed",
        ),
        EventDefinition(
            "checkout_due",
            date_field="checkout_date",
            condition=lambda instance: instance.status in ["confirmed", "checked_in"],
        ),
        EventDefinition(
            "review_request",
            date_field="checkout_date",
            condition=lambda instance: instance.status == "completed",
        ),
        EventDefinition(
            "always_event", date_field="checkin_date"  # No condition = always valid
        ),
        EventDefinition(
            "booking_confirmed",  # Immediate event
            condition=lambda instance: instance.status == "confirmed",
        ),
    ]


class TestOrder(models.Model):
    """Test model with immediate events only"""

    total = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, default="pending")

    events = [
        EventDefinition(
            "order_placed",  # Immediate event
            condition=lambda instance: instance.status == "confirmed",
        ),
        EventDefinition(
            "high_value_alert",  # Immediate event
            condition=lambda instance: instance.total > 1000,
        ),
    ]


class TestProperty(models.Model):
    """Test model representing a property with maintenance events"""

    name = models.CharField(max_length=100)
    maintenance_due = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default="active")
    events = [
        EventDefinition(
            "maintenance_due",
            date_field="maintenance_due",
            condition=lambda instance: instance.maintenance_due
            and instance.status == "active",
        )
    ]


class TestModelWithoutEvents(models.Model):
    """Test model with no events attribute - should not create any events"""
    name = models.CharField(max_length=100)


class Guest(models.Model):
    email = models.EmailField()
    name = models.CharField(max_length=100)
    vip_status = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.name} ({self.email})"


class Booking(models.Model):
    guest = models.ForeignKey(Guest, on_delete=models.CASCADE)
    checkin_date = models.DateTimeField()
    checkout_date = models.DateTimeField()
    room_number = models.CharField(max_length=10, blank=True)
    status = models.CharField(max_length=20, default="pending")
    payment_confirmed = models.BooleanField(default=False)
    special_requests = models.TextField(blank=True)

    # Events for this model
    events = [
        # Immediate events - occur right after save when condition is true
        EventDefinition("booking_created", condition=lambda b: True),
        EventDefinition(
            "booking_confirmed", condition=lambda b: b.status == "confirmed"
        ),
        EventDefinition("payment_received", condition=lambda b: b.payment_confirmed),
        EventDefinition(
            "guest_checked_in", condition=lambda b: b.status == "checked_in"
        ),
        EventDefinition(
            "housekeeping_confirmed",
            condition=lambda b: b.status == "housekeeping_ready",
        ),
        # Scheduled events - occur at specific datetime when condition is true
        EventDefinition(
            "checkin_reminder",
            date_field="checkin_date",
            condition=lambda b: b.status == "confirmed",
        ),
        EventDefinition(
            "checkin_due",
            date_field="checkin_date",
            condition=lambda b: b.status == "confirmed",
        ),
        EventDefinition(
            "checkout_due",
            date_field="checkout_date",
            condition=lambda b: b.status == "confirmed",
        ),
        # Agent-friendly aliases
        EventDefinition(
            "move_in",
            date_field="checkin_date",
            condition=lambda b: b.status == "confirmed",
        ),
        EventDefinition(
            "move_out",
            date_field="checkout_date",
            condition=lambda b: b.status == "confirmed",
        ),
    ]

    def __str__(self):
        return f"Booking {self.id} - {self.guest.name} ({self.status})"


class HousekeepingTask(models.Model):
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE)
    task_type = models.CharField(max_length=50)
    completed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.task_type} for {self.booking}"


class TestUUIDModel(models.Model):
    """Test model with UUID primary key to verify event entity_id casting"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    due_date = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)

    events = [
        EventDefinition(
            "uuid_scheduled_event",
            date_field="due_date",
            condition=lambda instance: instance.active,
        ),
        EventDefinition(
            "uuid_immediate_event",
            condition=lambda instance: instance.active,
        ),
    ]


class TestTriggerModel(models.Model):
    """Test model for trigger behavior (CREATE, UPDATE, DELETE)"""
    name = models.CharField(max_length=100)
    status = models.CharField(max_length=20, default="active")

    events = [
        # Only fires on creation
        EventDefinition(
            "entity_created",
            condition=lambda instance: True,
            trigger=EventTrigger.CREATE,
        ),
        # Only fires on updates (not creation)
        EventDefinition(
            "entity_modified",
            condition=lambda instance: instance.status == "active",
            trigger=EventTrigger.UPDATE,
        ),
        # Fires on both create and update
        EventDefinition(
            "entity_saved",
            condition=lambda instance: True,
            trigger=[EventTrigger.CREATE, EventTrigger.UPDATE],
        ),
        # Only fires on deletion
        EventDefinition(
            "entity_deleted",
            condition=lambda instance: True,
            trigger=EventTrigger.DELETE,
        ),
    ]
