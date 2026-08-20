"""Fallback automation layer (priorities 4 and 5) and the operation router."""

from app.automation.input_automation import InputAutomation
from app.automation.strategy import Attempt, OperationOutcome, OperationRouter, Tier
from app.automation.ui_automation import UIAutomation
from app.automation.vision_automation import Match, VisionAutomation, grab_screen, match_template

__all__ = [
    "Tier",
    "Attempt",
    "OperationRouter",
    "OperationOutcome",
    "UIAutomation",
    "VisionAutomation",
    "Match",
    "grab_screen",
    "match_template",
    "InputAutomation",
]
