"""chat — EVA rule-based reply engine for Chitchat.gg.

Quick start::

    from chat import ChatRuleBot

    bot = ChatRuleBot()
    state = bot.new_conversation()
    print(bot.reply("hey", state))
    print(bot.reply("where r u from", state))
"""

from .rule_bot import ChatRuleBot
from .rules import RuleEngine
from .common_scan import scan_message
from .style_analyzer import StyleProfile, analyze_style, mirror_reply
from .geo_handler import CountryRotator, is_high_priority
from .database import load_pipe_db, load_evoflow_rules, load_all_databases

__all__ = [
    "ChatRuleBot",
    "RuleEngine",
    "scan_message",
    "StyleProfile",
    "analyze_style",
    "mirror_reply",
    "CountryRotator",
    "is_high_priority",
    "load_pipe_db",
    "load_evoflow_rules",
    "load_all_databases",
]
