"""
Tests for the dynamic tool system.
"""
import inspect
from typing import Optional
from django.test import TestCase
from django_ai.conversations.decorators import with_context
from .registry import register_tool, get_tool, get_tool_metadata, list_tools, tool_exists, _tool_registry


class ToolRegistryTests(TestCase):
    """Test tool registration and retrieval"""

    def setUp(self):
        """Clear registry before each test"""
        _tool_registry.clear()

    def test_basic_registration(self):
        """Test basic tool registration with manual metadata"""
        @register_tool(
            title="My Custom Tool",
            description="Does something useful",
            icon="star",
            category="test"
        )
        def my_tool(arg1: int, arg2: str = "default") -> str:
            """Original docstring."""
            return f"{arg1}-{arg2}"

        # Tool exists
        self.assertTrue(tool_exists("my_tool"))

        # Metadata is correct
        metadata = get_tool_metadata("my_tool")
        self.assertEqual(metadata["name"], "my_tool")
        self.assertEqual(metadata["title"], "My Custom Tool")
        self.assertEqual(metadata["description"], "Does something useful")
        self.assertEqual(metadata["icon"], "star")
        self.assertEqual(metadata["category"], "test")

        # Function is retrievable and works
        func = get_tool("my_tool")
        self.assertEqual(func(123, "test"), "123-test")

    def test_auto_generation(self):
        """Test auto-generation of title and description"""
        @register_tool(icon="file")
        def get_file_content(file_id: int) -> str:
            """Get the OCR-extracted content from a managed file."""
            return "content"

        metadata = get_tool_metadata("get_file_content")

        # Auto-generated title: "get_file_content" -> "Get File Content"
        self.assertEqual(metadata["title"], "Get File Content")

        # Auto-generated description from first line of docstring
        self.assertEqual(
            metadata["description"],
            "Get the OCR-extracted content from a managed file."
        )

    def test_signature_preservation(self):
        """Test that function signature is preserved"""
        @register_tool(icon="test")
        def complex_function(
            required: int,
            optional: Optional[str] = None,
            keyword_only: bool = False
        ) -> dict:
            """Test function with complex signature."""
            return {"required": required, "optional": optional, "keyword_only": keyword_only}

        # Get the registered function
        func = get_tool("complex_function")

        # Check signature is preserved
        sig = inspect.signature(func)
        params = sig.parameters

        self.assertEqual(len(params), 3)
        self.assertEqual(params["required"].annotation, int)
        self.assertEqual(params["optional"].annotation, Optional[str])
        self.assertEqual(params["optional"].default, None)
        self.assertEqual(params["keyword_only"].annotation, bool)
        self.assertEqual(params["keyword_only"].default, False)

        # Check return annotation
        self.assertEqual(sig.return_annotation, dict)

        # Check docstring
        self.assertEqual(func.__doc__, "Test function with complex signature.")

    def test_stacked_decorators_preserve_metadata(self):
        """Test that @register_tool + @with_context preserves original metadata"""
        @register_tool(icon="stacked", category="test")
        @with_context()
        def stacked_tool(user_id: int, table_id: Optional[int] = None) -> str:
            """
            A tool with context injection.

            Args:
                user_id: The user ID
                table_id: Optional table ID

            Returns:
                A formatted string
            """
            return f"user:{user_id}, table:{table_id}"

        # Get the registered function
        func = get_tool("stacked_tool")

        # Check function name preserved
        self.assertEqual(func.__name__, "stacked_tool")

        # Check signature preserved (original args, not wrapper args)
        sig = inspect.signature(func)
        params = sig.parameters

        self.assertIn("user_id", params)
        self.assertIn("table_id", params)
        self.assertEqual(params["user_id"].annotation, int)
        self.assertEqual(params["table_id"].annotation, Optional[int])
        self.assertEqual(params["table_id"].default, None)

        # Check return type preserved
        self.assertEqual(sig.return_annotation, str)

        # Check docstring preserved (full original docstring)
        self.assertIn("A tool with context injection", func.__doc__)
        self.assertIn("Args:", func.__doc__)
        self.assertIn("user_id: The user ID", func.__doc__)

        # Check annotations preserved
        self.assertEqual(func.__annotations__["user_id"], int)
        self.assertEqual(func.__annotations__["table_id"], Optional[int])
        self.assertEqual(func.__annotations__["return"], str)

        # Verify metadata in registry also has correct signature
        metadata = get_tool_metadata("stacked_tool")
        registry_sig = metadata["signature"]
        self.assertEqual(len(registry_sig.parameters), 2)
        self.assertIn("user_id", registry_sig.parameters)
        self.assertIn("table_id", registry_sig.parameters)

    def test_use_cases(self):
        """Test that use_cases are stored correctly"""
        @register_tool(
            icon="search",
            use_cases=["find data", "search database", "query information"]
        )
        def search_tool(query: str) -> list:
            """Search for information."""
            return []

        metadata = get_tool_metadata("search_tool")
        self.assertEqual(
            metadata["use_cases"],
            ["find data", "search database", "query information"]
        )

    def test_list_tools_filtering(self):
        """Test listing tools with category filter"""
        @register_tool(icon="file", category="files")
        def file_tool():
            """File tool."""
            pass

        @register_tool(icon="table", category="data")
        def data_tool():
            """Data tool."""
            pass

        # List all tools
        all_tools = list_tools()
        self.assertEqual(len(all_tools), 2)
        self.assertIn("file_tool", all_tools)
        self.assertIn("data_tool", all_tools)

        # List by category
        file_tools = list_tools(category="files")
        self.assertEqual(len(file_tools), 1)
        self.assertIn("file_tool", file_tools)

        data_tools = list_tools(category="data")
        self.assertEqual(len(data_tools), 1)
        self.assertIn("data_tool", data_tools)

    def test_tool_not_found_error(self):
        """Test that getting non-existent tool raises ValueError"""
        with self.assertRaises(ValueError) as context:
            get_tool("nonexistent_tool")

        self.assertIn("not registered", str(context.exception))

        with self.assertRaises(ValueError) as context:
            get_tool_metadata("nonexistent_tool")

        self.assertIn("not registered", str(context.exception))

    def test_tool_function_unchanged(self):
        """Test that @register_tool doesn't wrap the function"""
        def original_function(x: int) -> int:
            """Original function."""
            return x * 2

        # Store reference to original
        original_id = id(original_function)

        # Register it
        decorated = register_tool(icon="test")(original_function)

        # Should be the exact same object (not wrapped)
        self.assertEqual(id(decorated), original_id)
        self.assertIs(decorated, original_function)

    def test_module_storage(self):
        """Test that module path is stored in metadata"""
        @register_tool(icon="test")
        def test_module_func():
            """Test function."""
            pass

        metadata = get_tool_metadata("test_module_func")
        self.assertIn("module", metadata)
        self.assertEqual(metadata["module"], "django_ai.tools.tests")


class ToolReferenceNormalizationTests(TestCase):
    """Test normalize_tool_reference function"""

    def setUp(self):
        """Clear registry and register test tools"""
        from .registry import _tool_registry, normalize_tool_reference
        _tool_registry.clear()

        # Register a test tool
        @register_tool(icon="test")
        def my_test_tool(arg: int) -> str:
            """Test tool."""
            return f"result-{arg}"

        self.my_test_tool = my_test_tool
        self.normalize = normalize_tool_reference

    def test_normalize_string_reference(self):
        """Test normalizing string tool reference"""
        result = self.normalize("my_test_tool")
        self.assertEqual(result, "my_test_tool")

    def test_normalize_function_reference(self):
        """Test normalizing function tool reference"""
        result = self.normalize(self.my_test_tool)
        self.assertEqual(result, "my_test_tool")

    def test_normalize_unregistered_function(self):
        """Test that unregistered function raises error"""
        def unregistered_func():
            pass

        with self.assertRaises(ValueError) as context:
            self.normalize(unregistered_func)

        self.assertIn("not registered", str(context.exception))

    def test_normalize_invalid_type(self):
        """Test that invalid type raises error"""
        with self.assertRaises(ValueError) as context:
            self.normalize(123)

        self.assertIn("must be a string or callable", str(context.exception))

    def test_agent_with_function_references(self):
        """Test that agent can use function references in Tools.base"""
        from django_ai.conversations.base import ConversationAgent
        from pydantic import BaseModel

        class TestAgent(ConversationAgent):
            class Tools:
                base = [self.my_test_tool, "my_test_tool"]  # Mix of function and string
                available = []  # No additional tools available
                max_additional = 3

            class Context(BaseModel):
                user_id: int

            @classmethod
            def create_context(cls, request=None, **kwargs):
                return cls.Context(**kwargs)

            def get_response(self, message, request=None, **kwargs):
                return "response"

        # Should initialize without error and normalize both references
        agent = TestAgent()

        # Both should be normalized to strings
        self.assertEqual(len(agent.Tools.base), 2)
        self.assertEqual(agent.Tools.base[0], "my_test_tool")
        self.assertEqual(agent.Tools.base[1], "my_test_tool")

    def test_agent_tools_validation_invalid_keys(self):
        """Test that Tools class with invalid keys raises error"""
        from django_ai.conversations.base import ConversationAgent
        from pydantic import BaseModel

        class BadAgent(ConversationAgent):
            class Tools:
                bases = ["tool1"]  # Typo! Should be 'base'
                max_tools = 3  # Typo! Should be 'max_additional'
                available = []

            class Context(BaseModel):
                user_id: int

            @classmethod
            def create_context(cls, request=None, **kwargs):
                return cls.Context(**kwargs)

            def get_response(self, message, request=None, **kwargs):
                return "response"

        # Should raise error during initialization
        with self.assertRaises(ValueError) as context:
            BadAgent()

        error_msg = str(context.exception)
        self.assertIn("invalid attributes", error_msg)
        self.assertIn("bases", error_msg)
        self.assertIn("max_tools", error_msg)

    def test_agent_tools_validation_overlap(self):
        """Test that available tools cannot overlap with base tools"""
        from django_ai.conversations.base import ConversationAgent
        from pydantic import BaseModel

        @register_tool(icon="test")
        def overlap_tool():
            """Test tool."""
            pass

        class OverlapAgent(ConversationAgent):
            class Tools:
                base = ["overlap_tool"]
                available = ["overlap_tool"]  # Error! Already in base
                max_additional = 3

            class Context(BaseModel):
                user_id: int

            @classmethod
            def create_context(cls, request=None, **kwargs):
                return cls.Context(**kwargs)

            def get_response(self, message, request=None, **kwargs):
                return "response"

        # Should raise error during initialization
        with self.assertRaises(ValueError) as context:
            OverlapAgent()

        error_msg = str(context.exception)
        self.assertIn("available contains base tools", error_msg)
        self.assertIn("overlap_tool", error_msg)


class ToolLoadoutIntegrationTests(TestCase):
    """Test the complete tool system with ToolLoadout"""

    def setUp(self):
        """Set up test tools and clear registry"""
        from .registry import _tool_registry
        _tool_registry.clear()

        # Register test tools
        @register_tool(icon="test", category="base")
        def base_tool_1():
            """Base tool 1."""
            return "base1"

        @register_tool(icon="test", category="base")
        def base_tool_2():
            """Base tool 2."""
            return "base2"

        @register_tool(icon="test", category="additional")
        def additional_tool_1():
            """Additional tool 1."""
            return "add1"

        @register_tool(icon="test", category="additional")
        def additional_tool_2():
            """Additional tool 2."""
            return "add2"

        self.base_tool_1 = base_tool_1
        self.base_tool_2 = base_tool_2
        self.additional_tool_1 = additional_tool_1
        self.additional_tool_2 = additional_tool_2

    def test_happy_path_with_loadout(self):
        """Test complete flow: agent with base tools + ToolLoadout with additional tools"""
        from django_ai.conversations.base import ConversationAgent
        from django_ai.tools.models import ToolLoadout
        from pydantic import BaseModel

        # Define agent with base and available tools
        class TestAgent(ConversationAgent):
            class Tools:
                base = ["base_tool_1", "base_tool_2"]
                available = ["additional_tool_1", "additional_tool_2"]
                max_additional = 2

            class Context(BaseModel):
                user_id: int

            @classmethod
            def create_context(cls, request=None, **kwargs):
                return cls.Context(**kwargs)

            def get_response(self, message, request=None, **kwargs):
                return "response"

        # Create agent
        agent = TestAgent()

        # Verify base tools are set correctly
        self.assertEqual(agent.Tools.base, ["base_tool_1", "base_tool_2"])
        self.assertEqual(agent.Tools.available, ["additional_tool_1", "additional_tool_2"])

        # Create a ToolLoadout with additional tools
        loadout = ToolLoadout.objects.create(
            additional_tools=["additional_tool_1"]
        )

        # Validate it works with the agent
        loadout.validate_for_agent(TestAgent)  # Should not raise

        # Verify we can get available and max
        available = loadout.get_available_tools(TestAgent)
        self.assertEqual(available, ["additional_tool_1", "additional_tool_2"])

        max_additional = loadout.get_max_additional_tools(TestAgent)
        self.assertEqual(max_additional, 2)

    def test_loadout_validation_errors(self):
        """Test that ToolLoadout validates correctly"""
        from django_ai.conversations.base import ConversationAgent
        from django_ai.tools.models import ToolLoadout
        from django.core.exceptions import ValidationError
        from pydantic import BaseModel

        class TestAgent(ConversationAgent):
            class Tools:
                base = ["base_tool_1"]
                available = ["additional_tool_1"]
                max_additional = 1

            class Context(BaseModel):
                user_id: int

            @classmethod
            def create_context(cls, request=None, **kwargs):
                return cls.Context(**kwargs)

            def get_response(self, message, request=None, **kwargs):
                return "response"

        # Test 1: Tool not in available list
        loadout = ToolLoadout.objects.create(
            additional_tools=["additional_tool_2"]  # Not in available!
        )

        with self.assertRaises(ValidationError) as context:
            loadout.validate_for_agent(TestAgent)

        self.assertIn("unavailable tools", str(context.exception))

        # Test 2: Too many tools
        loadout2 = ToolLoadout.objects.create(
            additional_tools=["additional_tool_1", "additional_tool_1"]  # 2 tools, max is 1
        )

        with self.assertRaises(ValidationError) as context:
            loadout2.validate_for_agent(TestAgent)

        self.assertIn("Too many", str(context.exception))
