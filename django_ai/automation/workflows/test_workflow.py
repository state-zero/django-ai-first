"""
Simple test workflow for development
"""
from pydantic import BaseModel
from rest_framework import serializers
from django_ai.automation.workflows.core import workflow, step,  goto, complete
from django_ai.automation.workflows.statezero_action import statezero_action
from statezero.core.classes import DisplayMetadata, FieldGroup

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
    
    @statezero_action(
        name="collect_user_info",
        serializer=UserInfoSerializer,
        display=DisplayMetadata(
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
        ))
    @step()
    def collect_user_info(self, name: str, email: str, favorite_color: str):
        """Collect user information"""
        
        self.context.name = name
        self.context.email = email
        self.context.favorite_color = favorite_color
        return goto(self.create_account, progress=0.5)
    
    @step()
    def create_account(self):
        """Automated step: create account"""
        
        # Simulate account creation
        self.context.account_id = f"ACC-{hash(self.context.email) % 100000}"
        return goto(self.setup_preferences, progress=0.75)
    
    @step()
    def setup_preferences(self):
        """Automated step: set up user preferences"""
        
        # Simulate preference setup
        self.context.setup_complete = True
        return goto(self.finalize, progress=0.9)
    
    @step()
    def finalize(self):
        """Complete the workflow"""
        
        return complete(
            display_title=f"Welcome aboard, {self.context.name}! 🎉",
            display_subtitle=f"Your account {self.context.account_id} is ready. We've set your theme to {self.context.favorite_color}."
        )


# To start a test run in Django shell:
# from django_ai.automation.workflows.core import engine
# run = engine.start("user_onboarding")
# print(f"Started workflow run: {run.id}")