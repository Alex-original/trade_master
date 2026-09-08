"""自然语言 → 策略结构化解析（NL to UI 的解析端）。"""
from .parser import parse_strategy, ParseResult, validate_dict
from .rules import match_local, RuleHit

__all__ = ["parse_strategy", "ParseResult", "validate_dict", "match_local", "RuleHit"]
