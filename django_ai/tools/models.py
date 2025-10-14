from django.db import models
from django.core.exceptions import ValidationError


class ToolLoadout(models.Model):
    """
    A tool loadout configuration.

    Defines which additional tools are selected for a specific context
    (e.g., a conversation session, a workflow run, etc.)
    """

    additional_tools = models.JSONField(
        default=list,
        help_text="List of additional tool names beyond base tools"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Tool Loadout"
        verbose_name_plural = "Tool Loadouts"

    def __str__(self):
        return f"ToolLoadout {self.id} ({len(self.additional_tools)} tools)"

    def get_available_tools(self, agent_class):
        """
        Get list of tools available for this loadout based on agent class.

        Args:
            agent_class: The ConversationAgent class

        Returns:
            List of tool names (strings)
        """
        if hasattr(agent_class, 'Tools'):
            return agent_class.Tools.available
        return []

    def get_max_additional_tools(self, agent_class):
        """
        Get the maximum number of additional tools allowed for this agent.

        Args:
            agent_class: The ConversationAgent class

        Returns:
            int or None (None = unlimited)
        """
        if hasattr(agent_class, 'Tools'):
            return agent_class.Tools.max_additional
        return 5  # Default

    def validate_for_agent(self, agent_class):
        """
        Validate that additional_tools are valid for the given agent class.

        Args:
            agent_class: The ConversationAgent class

        Raises:
            ValidationError: If validation fails
        """
        errors = {}

        # Get available tools for this agent
        available = self.get_available_tools(agent_class)

        # Validate tools exist and are available
        invalid_tools = []
        for tool_name in self.additional_tools:
            if tool_name not in available:
                invalid_tools.append(tool_name)

        if invalid_tools:
            errors['additional_tools'] = (
                f"Unknown or unavailable tools: {', '.join(invalid_tools)}. "
                f"Available tools for this agent: {', '.join(available) if available else 'none'}"
            )

        # Validate max_additional_tools limit
        max_tools = self.get_max_additional_tools(agent_class)
        if max_tools is not None and len(self.additional_tools) > max_tools:
            errors['additional_tools'] = (
                f"Too many additional tools. This agent allows a maximum of {max_tools} "
                f"additional tool(s), but {len(self.additional_tools)} were provided."
            )

        if errors:
            raise ValidationError(errors)
