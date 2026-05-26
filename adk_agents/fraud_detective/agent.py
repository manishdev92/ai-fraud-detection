"""ADK CLI entrypoint: adk web adk_agents/fraud_detective"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.agents import root_agent

__all__ = ["root_agent"]
