from dataclasses import dataclass
from typing import List, Optional, Dict, Any

@dataclass
class FieldDisplayConfig:
    field_name: str
    display_component: Optional[str] = None
    filter_queryset: Optional[Dict[str, Any]] = None
    display_help_text: Optional[str] = None
    
@dataclass
class FieldGroup:
    display_title: str
    display_description: Optional[str] = None
    field_names: List[str] = None
    
@dataclass
class StepDisplayMetadata:
    display_title: str
    display_description: Optional[str] = None
    field_groups: Optional[List[FieldGroup]] = None
    field_display_configs: Optional[List[FieldDisplayConfig]] = None