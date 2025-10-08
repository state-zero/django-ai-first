"""
Simple test workflow for development
"""
from pydantic import BaseModel
from rest_framework import serializers
from django_ai.automation.workflows.core import workflow, step, get_context, goto, complete
from django_ai.automation.workflows.statezero_action import statezero_action
from django_ai.automation.workflows.metadata import StepDisplayMetadata, FieldGroup


class UserInfoSerializer(serializers.Serializer):
    """Serializer for user information input"""
    workflow_run_id = serializers.IntegerField()
    name = serializers.CharField(max_length=100, help_text="Your full name")
    email = serializers.EmailField(help_text="Your email address")
    favorite_color = serializers.ChoiceField(
        choices=[
            ('red', 'Red'),
            ('blue', 'Blue'),
            ('green', 'Green'),
            ('yellow', 'Yellow'),
        ],
        help_text="Pick your favorite color"
    )


@workflow("user_onboarding")
class UserOnboardingWorkflow:
    """
    Simple workflow for testing the UI components.
    Demonstrates all major workflow states: action, automated, and completion.
    """
    
    class Context(BaseModel):
        name: str = ""
        email: str = ""
        favorite_color: str = ""
        account_id: str = ""
        setup_complete: bool = False
    
    @step(start=True)
    def initialize(self):
        """Initialize the workflow"""
        return goto(self.collect_user_info, progress=0.2)
    
    @statezero_action(name="collect_user_info", serializer=UserInfoSerializer)
    @step(
        display=StepDisplayMetadata(
            display_title="Welcome! Let's Get Started",
            display_description="Please provide some basic information to set up your account",
            field_groups=[
                FieldGroup(
                    display_title="Personal Information",
                    display_description="We'll use this to personalize your experience",
                    field_names=["name", "email"]
                ),
                FieldGroup(
                    display_title="Preferences",
                    display_description="Help us customize your interface",
                    field_names=["favorite_color"]
                )
            ]
        )
    )
    def collect_user_info(self, name: str, email: str, favorite_color: str):
        """Collect user information"""
        ctx = get_context()
        ctx.name = name
        ctx.email = email
        ctx.favorite_color = favorite_color
        return goto(self.create_account, progress=0.5)
    
    @step()
    def create_account(self):
        """Automated step: create account"""
        ctx = get_context()
        # Simulate account creation
        ctx.account_id = f"ACC-{hash(ctx.email) % 100000}"
        return goto(self.setup_preferences, progress=0.75)
    
    @step()
    def setup_preferences(self):
        """Automated step: set up user preferences"""
        ctx = get_context()
        # Simulate preference setup
        ctx.setup_complete = True
        return goto(self.finalize, progress=0.9)
    
    @step()
    def finalize(self):
        """Complete the workflow"""
        ctx = get_context()
        return complete(
            display_title=f"Welcome aboard, {ctx.name}! 🎉",
            display_subtitle=f"Your account {ctx.account_id} is ready. We've set your theme to {ctx.favorite_color}."
        )


# To start a test run in Django shell:
# from django_ai.automation.workflows.core import engine
# run = engine.start("user_onboarding")
# print(f"Started workflow run: {run.id}")