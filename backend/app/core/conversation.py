"""Conversational gate: a greeting deserves an answer, not a reconnaissance run.

The operator saying ``hello`` is not a mission.  :func:`is_smalltalk` recognises
pure greetings, pleasantries, identity questions and one-word acknowledgements so
the orchestrator can answer like a person and ask what the mission is, with tools
switched off for that turn.  Anything carrying an operational signal - a verb
(scan, exploit, report...), a target (IP, domain, URL, path, port) - always falls
through to the normal tool loop, so a greeting followed by real work still works.
"""
from __future__ import annotations

import re

#: Canned reply used when the model returns no text at all in conversation mode.
CONVERSATIONAL_FALLBACK = (
    "Hey - good to meet you. I'm Mission Control for Anonymous V2: I can map a target, test web "
    "applications, validate exploits, write tooling and put the results in a report. "
    "What's the mission - what should I look at, and what do you want to know about it?"
)

#: Extra system instruction injected for the greeting turn only.
#: No tool is offered and none is executed, so this cannot be undone by the model.
CONVERSATIONAL_INSTRUCTIONS = (
    "CONVERSATION MODE: the operator only greeted you, thanked you or asked who you are. Answer the way a person "
    "would in one to three sentences: acknowledge them, say briefly what you are and what you can do, then ask "
    "what the mission is (target, goal, anything they already know). If earlier turns already established a "
    "mission, ask what to do next instead of starting over. Do not call any tool, do not scan, do not delegate, "
    "do not announce a plan. Wait for the objective before doing any work."
)

_APOSTROPHES = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u02bc": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u00b4": "'",
    }
)

#: Openers that only make sense as social noise.
_GREETINGS = frozenset(
    {
        "hi",
        "hii",
        "hiya",
        "hey",
        "heya",
        "hello",
        "helo",
        "yo",
        "sup",
        "howdy",
        "greetings",
        "hola",
        "salut",
        "bonjour",
        "bonsoir",
        "coucou",
        "ciao",
        "hallo",
        "aloha",
        "namaste",
        "morning",
        "afternoon",
        "evening",
    }
)

#: Pure pleasantries / acknowledgements, matched against the whole message.
_PLEASANTRY = re.compile(
    r"^(?:"
    r"thanks|thank you|thx|ty|cheers|ok|okay|kk|cool|nice|great|awesome|perfect|"
    r"got it|sounds good|nice one|no worries|no problem|not yet|nothing yet|"
    r"stand by|standby|one moment|hold on|bye|goodbye|see you|cya|later|good night|goodnight"
    r")(?: (?:you|then|mate|chief|team|all|for now|so much|very much|a lot|again))?$"
)

#: Identity / capability questions, which are conversation rather than a task.
_IDENTITY = re.compile(
    r"^(?:"
    r"who are you|what are you|what is this|what is anonymous(?: v2)?|what can you do|what do you do|"
    r"what are your (?:tools|capabilities)|how do(?:es)? (?:this|it|you) work|help|"
    r"help me(?: get started)?|getting started|introduce yourself"
    r")$"
)

#: Chit-chat that is not a greeting word but still carries no work.
_CHITCHAT = re.compile(
    r"^(?:"
    r"how are you(?: doing)?|how'?s it going|how is it going|what'?s up|whats up|"
    r"you (?:there|awake|around|busy)|are you (?:there|awake|online|ready)|"
    r"good to (?:see|meet) you|nice to meet you"
    r")$"
)

#: Verbs that mean the operator is asking for work, not making conversation.
_WORK_SIGNALS = re.compile(
    r"\b(?:"
    r"scan|scans|scanning|nmap|masscan|rustscan|recon|reconnaissance|enumerate|enumeration|fingerprint|"
    r"fuzz|fuzzing|brute|bruteforce|brute-force|crack|exploit|exploitation|attack|hack|pwn|pentest|"
    r"inject|injection|xss|sqli|sql|rce|lfi|ssrf|xxe|csrf|payload|reverse-shell|shell|"
    r"vuln|vulns|vulnerability|vulnerabilities|cve|cwe|misconfiguration|bypass|privesc|"
    r"target|targets|host|hosts|domain|domains|subdomain|subdomains|ip|port|ports|url|urls|"
    r"endpoint|endpoints|service|services|network|subnet|server|servers|website|webapp|api|"
    r"database|credential|credentials|password|passwords|hash|hashes|"
    r"curl|wget|netcat|ssh|ftp|smb|samba|dns|whois|dig|nikto|ffuf|gobuster|nuclei|sqlmap|hydra|"
    r"metasploit|msfconsole|burp|bloodhound|cobalt|"
    r"explore|investigate|check|verify|validate|assess|map|audit|review|test|find|search|gather|"
    r"write|build|create|script|report|summarize|summarise|analyze|analyse|dump|extract|collect|"
    r"run|start|begin|launch|perform|execute|demonstrate|prove|look"
    r")\b"
)

#: Targets and paths: if one of these appears, the operator means business.
_TARGET_SIGNALS = re.compile(
    r"(?:"
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b"  # IPv4
    r"|\bhttps?://|www\."  # URL
    r"|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|dev|app|local|internal|test|xyz|info|biz|me|co|us|uk|ai|sh|ru|cn|onion)\b"
    r"|:\d{2,5}\b"  # host:port
    r"|[/\\][a-z0-9_.-]+"  # file path
    r")"
)


def _normalise(message: str) -> str:
    """Lower-case, strip wrapping punctuation and collapse whitespace."""
    text = (message or "").translate(_APOSTROPHES).lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n.,!?;:~*_\"'`()[]{}<>")


def is_smalltalk(message: str) -> bool:
    """Whether ``message`` is greeting / chit-chat with no operational content."""
    text = _normalise(message)
    if not text:
        return True
    if len(text) > 160:
        return False
    words = re.findall(r"[a-z0-9'][a-z0-9'-]*", text)
    if not words:
        return True
    if _IDENTITY.fullmatch(text) or _CHITCHAT.fullmatch(text) or _PLEASANTRY.fullmatch(text):
        return True
    if _WORK_SIGNALS.search(text) or _TARGET_SIGNALS.search(text):
        return False
    if words[0] in _GREETINGS:
        # "hello", "hi there", "hey, you around?" - greetings may carry a few filler words.
        return len(words) <= 6
    return len(words) <= 2


__all__ = [
    "CONVERSATIONAL_FALLBACK",
    "CONVERSATIONAL_INSTRUCTIONS",
    "is_smalltalk",
]
