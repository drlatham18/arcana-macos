from __future__ import annotations

import re
from dataclasses import dataclass

# Labels that spend real money or account currency, or that hand cards away.
# These are never presented as allowed, even when the client has them enabled.
CURRENCY_LABELS = (
    "buy", "purchase", "checkout", "add to cart", "redeem", "event ticket",
    "play point", "entry token", "sell", "bid", "trade",
)
# Labels that destroy something the user cannot get back by clicking again.
# "Leave Event" and "Drop" forfeit an event that is already paid for and in
# progress, so they belong here even though they read as harmless navigation.
DESTRUCTIVE_LABELS = (
    "delete", "unregister", "concede", "resign", "remove post",
    "leave event", "drop from", "forfeit", "quit match",
)
# Outside a match these commit an event entry or a payment. Inside a match the
# same words are ordinary gameplay: "pay" is mana payment and "confirm" answers
# a prompt. Context decides, so they are classified separately.
ENTRY_LABELS = ("pay", "confirm", "submit", "enter event", "join", "sign up", "register")

# MTGO's match buttons are frequently missing from the UI Automation tree even
# when their prompt text is exposed, so these are also matched against raw OCR.
MATCH_ACTIONS = (
    "no response", "yield", "take action", "declare attackers", "declare blockers",
    "next turn", "keep", "mulligan", "resolve", "pass", "done", "ok", "cancel",
    "draw another card", "draw another hand", "close",
)

# Ranked best-next-action. Earlier entries win when several are available.
# This deliberately does name a concrete move, including on judgement calls such
# as keep/mulligan. A player learning the game needs to be told what a reasonable
# move looks like; the per-action note still says the choice is theirs.
RECOMMENDATION_ORDER = (
    "keep", "mulligan", "declare attackers", "declare blockers", "no response",
    "take action", "next turn", "yield", "pass", "resolve", "ok", "done",
    "close", "cancel",
)

# Window chrome and Telerik docking internals are real controls, but pressing
# them closes or rearranges MTGO rather than playing it. They are not actions.
NOISE_MARKERS = (
    "titlebar", "maximizebutton", "minimizebutton", "restorebutton",
    "telerik.", "shiny.", "system.windows.", "raddocumentpane", "radpanegroup",
    "radsplitcontainer", "raddocking", "autohide", "mainwindowdockinghost",
    "mainwindowpane", "mainsplitcontainer",
)

ACTION_NOTES = {
    "no response": "Decline to act and let the current spell or ability resolve. This is not conceding.",
    "yield": "Pass priority. You keep priority again at the next stop unless you yield the turn.",
    "take action": "Open your available responses at this priority stop.",
    "declare attackers": "Confirm the attackers you have selected. Untapped creatures without summoning sickness are normally eligible.",
    "declare blockers": "Confirm your blocking assignments. Blocking does not tap the blocker.",
    "next turn": "Advance past your remaining stops this turn.",
    "keep": "Keep this opening hand. Your decision, not the companion's.",
    "mulligan": "Draw a new opening hand and put one card back on resolution. Your decision, not the companion's.",
    "resolve": "Let the top object on the stack resolve.",
    "pass": "Pass priority without acting.",
    "ok": "Acknowledge the prompt and continue.",
    "done": "Finish the current selection and continue.",
    "cancel": "Back out of the current dialog or selection. Reversible.",
    "close": "Close this panel and return to the previous screen. Reversible.",
    "home": "Return to the MTGO home screen. Reversible.",
    "draw another card": "Read-only sample-hand control. It does not change your deck.",
    "draw another hand": "Read-only sample-hand control. It does not change your deck.",
}


@dataclass(frozen=True)
class PermittedActions:
    """What the current screen actually permits, split by consequence."""

    allowed: tuple[tuple[str, str], ...]
    blocked: tuple[tuple[str, str], ...]
    unavailable: tuple[tuple[str, str], ...]
    recommended: str
    source: str

    @property
    def is_empty(self) -> bool:
        return not (self.allowed or self.blocked or self.unavailable)


def classify_actions(
    window_title: str,
    screen_text: str,
    controls: object = None,
) -> PermittedActions:
    """Split every readable control into allowed, blocked, and unavailable.

    `controls` is an AccessibilitySnapshot's control tuple when one was read.
    It carries a real enabled flag, so it is preferred. Match windows often hide
    their buttons from UI Automation, so OCR text is merged in as well.
    """
    in_match = _looks_like_match(window_title, screen_text)
    found: list[tuple[str, bool]] = []
    source = "screen text"
    if controls:
        for control in controls:
            control_type = getattr(control, "control_type", "")
            if control_type not in ("Button", "TabItem", "MenuItem", "CheckBox", "ComboBox"):
                continue
            automation_id = (getattr(control, "automation_id", "") or "").strip()
            label = _clean(getattr(control, "name", "") or "") or _humanize(automation_id)
            if not label or _is_noise(label, automation_id):
                continue
            found.append((label, bool(getattr(control, "enabled", True))))
        if found:
            source = "accessibility tree"
    found.extend(_parse_control_lines(screen_text))
    if in_match:
        found.extend(_scan_match_actions(screen_text))
    # A consequential control the client renders but does not expose to UI
    # Automation must still be warned about, so raw text is scanned for it too.
    found.extend(_scan_consequential_text(screen_text, in_match))

    allowed: list[tuple[str, str]] = []
    blocked: list[tuple[str, str]] = []
    unavailable: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, enabled in found:
        key = label.casefold()
        if not label or key in seen:
            continue
        seen.add(key)
        reason = blocked_reason(label, in_match)
        if reason:
            blocked.append((label, reason))
        elif not enabled:
            unavailable.append((label, "Disabled by the client right now, so it cannot be pressed."))
        else:
            allowed.append((label, _note_for(label)))
    return PermittedActions(
        tuple(allowed[:8]),
        tuple(blocked[:6]),
        tuple(unavailable[:5]),
        _recommend(allowed, in_match),
        source,
    )


# How to answer each Arena engine request, and what that answer costs you.
# Arena hides its buttons from every reader except its own log, so these are
# written as the physical control the client shows for that request.
ARENA_RESPONSES = {
    "You have priority": [
        ("Pass priority", "Press the resolve/OK button, or Space. This is not conceding; the top of the stack then resolves."),
    ],
    "Declare attackers": [
        ("Attack with all", "The All Attack button declares every eligible creature as an attacker."),
        ("Attack with none", "Confirming with nothing selected declares no attackers and moves to the next step."),
    ],
    "Declare blockers": [
        ("Assign blockers", "Drag each blocker onto an attacker, then confirm. Blocking does not tap the blocker."),
        ("Take the damage", "Confirming with nothing assigned lets every attacker through unblocked."),
    ],
    "Keep or mulligan": [
        ("Keep", "Accept this hand. After a mulligan you put the required number of cards on the bottom."),
        ("Mulligan", "Shuffle back and draw seven again. The bottom-card cost grows with each mulligan."),
    ],
    "Pay a cost": [
        ("Accept the auto-tap", "Arena has proposed a payment. Confirm it, or click your own sources to pay differently."),
        ("Cancel", "Available while the payment is unfinished; it backs the cast out."),
    ],
    "Assign combat damage": [
        ("Order the blockers", "Drag the blockers into the order damage is assigned, then confirm."),
    ],
    "Choose targets": [
        ("Select a highlighted object", "Arena highlights exactly the objects it accepts as legal targets."),
    ],
    "Choose from a list": [
        ("Select from the list", "Pick the required number of entries, then confirm."),
    ],
    "The game is over": [
        ("Continue", "Acknowledge the result. Nothing here is reversible in the game that just ended."),
    ],
}
# One concrete move per request. These name a reasonable default rather than
# stopping at the decision, because the point is to show a player who does not
# yet know the options what a normal move looks like. The matching note in
# ARENA_RESPONSES always states what the move costs.
ARENA_RECOMMENDATIONS = {
    "You have priority": "Pass priority",
    "Declare attackers": "Attack with all",
    "Declare blockers": "Assign blockers",
    "Keep or mulligan": "Keep",
    "Pay a cost": "Accept the auto-tap",
    "Assign combat damage": "Order the blockers",
    "Choose targets": "Select a highlighted object",
    "Choose from a list": "Select from the list",
}


def classify_arena_actions(state: object) -> PermittedActions:
    """Build the permitted-action report from Arena's own engine action list.

    Arena's legal actions come from the Game Rules Engine, not from OCR, so
    they are exact. They are also incapable of spending money: an in-game
    action never touches gems, gold, or wildcards. That means card names are
    listed verbatim rather than being run through the currency word filter,
    which would otherwise refuse a card whose name contains "trade" or "buy".
    """
    if state is None:
        return PermittedActions((), (), (), "No Arena game is in progress", "Arena detailed log")
    if not getattr(state, "in_match", False):
        # Arena's menus expose nothing to any reader, so name the consequential
        # controls that are always on those screens rather than staying silent.
        return PermittedActions(
            (
                ("Play", "Opens the mode list. Choosing a mode does not by itself spend anything."),
                ("Decks", "Deck building and browsing are reversible."),
                ("Profile / Mastery / Settings", "Read-only screens."),
            ),
            (
                ("Store purchases", "Spends real money, gems, or gold."),
                ("Craft / Craft All", "Spends wildcards and cannot be undone."),
                ("Paid event entry", "Spends gems or gold; read the entry cost before confirming."),
            ),
            (),
            "Play",
            "Arena detailed log (no game in progress)",
        )

    allowed: list[tuple[str, str]] = []
    for action in getattr(state, "actions", ()):  # engine-legal plays
        note = action.detail or "Arena's rules engine currently accepts this action."
        allowed.append((action.label(), note))

    request = getattr(state, "request", "") or ""
    for label, note in ARENA_RESPONSES.get(request, []):
        allowed.append((label, note))

    unavailable: list[tuple[str, str]] = []
    if not getattr(state, "waiting_on_me", False):
        unavailable.append(("Anything at all", "Your opponent holds priority; Arena is not accepting input from you."))

    blocked = [
        ("Concede", "Ends the game immediately and cannot be undone."),
        ("Leave / Exit match", "Forfeits the game in progress the same way conceding does."),
    ]

    recommended = ARENA_RECOMMENDATIONS.get(request, "")
    if not recommended:
        recommended = allowed[0][0] if allowed else "Wait for Arena to ask you something"
    return PermittedActions(
        tuple(allowed[:10]),
        tuple(blocked),
        tuple(unavailable),
        recommended,
        "Arena detailed log (its own rules engine)",
    )


def render_actions(permitted: PermittedActions) -> str:
    """Plain-text permitted-action report for the chat pane."""
    if permitted is None or permitted.is_empty:
        return (
            "I could not read a single named control on this screen.\n"
            "1. Press `Esc` once to close any tooltip or card preview.\n"
            "2. Wait one second.\n"
            "3. Ask again so I can read a clean frame."
        )
    blocks: list[str] = []
    if permitted.allowed:
        lines = [f"• `{label}` — {note}" for label, note in permitted.allowed]
        blocks.append("You are allowed to do:\n" + "\n".join(lines))
    else:
        blocks.append("You are allowed to do:\nNothing safe and enabled was readable on this screen.")
    if permitted.blocked:
        lines = [f"• `{label}` — {reason}" for label, reason in permitted.blocked]
        blocks.append("Do not press:\n" + "\n".join(lines))
    if permitted.unavailable:
        lines = [f"• `{label}`" for label, _ in permitted.unavailable]
        blocks.append("Unavailable right now (disabled by the client):\n" + "\n".join(lines))
    blocks.append(f"If you want one action: `{permitted.recommended}`.")
    blocks.append(f"Read from the {permitted.source}. Anything the client hides is not in this list.")
    return "\n\n".join(blocks)


def blocked_reason(label: str, in_match: bool = False) -> str:
    """Return why a label must never be pressed automatically, or an empty string."""
    folded = label.casefold()
    for word in CURRENCY_LABELS:
        if word in folded:
            return "Can spend real money, Event Tickets, Play Points, or cards."
    for word in DESTRUCTIVE_LABELS:
        if word in folded:
            return "Destroys something that clicking again will not restore."
    if not in_match:
        # Whole-word only: "JoinedGames" lists games you are already in and must
        # not be mistaken for a "Join" that enters a paid event.
        for word in ENTRY_LABELS:
            if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", folded):
                return "Can commit an event entry or a payment from this screen."
    return ""


def _looks_like_match(window_title: str, screen_text: str) -> bool:
    title = window_title.casefold()
    if "(1-on-1)" in title or "game details" in title:
        return True
    text = screen_text.casefold()
    markers = ("your turn", "opponent", "declare attackers", "declare blockers",
               "mulligan", "no response", "next turn", "upkeep", "end step", "combat")
    return sum(marker in text for marker in markers) >= 2


def _parse_control_lines(text: str) -> list[tuple[str, bool]]:
    """Read the flattened accessibility dump, preserving the disabled marker."""
    results: list[tuple[str, bool]] = []
    pattern = r"(?im)^(Button|TabItem|MenuItem|CheckBox|ComboBox):\s*(.+?)\s*$"
    for match in re.finditer(pattern, text):
        raw = match.group(2)
        enabled = not re.search(r"\(disabled\)\s*$", raw, re.IGNORECASE)
        stripped = re.sub(r"\s*\(disabled\)\s*$", "", raw, flags=re.IGNORECASE)
        automation_id = ""
        id_match = re.search(r"\[([^\]]+)\]\s*$", stripped)
        if id_match:
            automation_id = id_match.group(1)
        label = _clean(stripped) or _humanize(automation_id)
        if label and not _is_noise(label, automation_id):
            results.append((label, enabled))
    return results


def _scan_match_actions(text: str) -> list[tuple[str, bool]]:
    """Recover match buttons that UI Automation exposed as prompt text only."""
    folded = text.casefold()
    results: list[tuple[str, bool]] = []
    for action in MATCH_ACTIONS:
        if re.search(rf"(?<!\w){re.escape(action)}(?!\w)", folded):
            results.append((action.title(), True))
    return results


def _scan_consequential_text(text: str, in_match: bool) -> list[tuple[str, bool]]:
    """Find money, trade, and concede words anywhere in the readable text."""
    folded = text.casefold()
    results: list[tuple[str, bool]] = []
    candidates = CURRENCY_LABELS + DESTRUCTIVE_LABELS
    if not in_match:
        candidates += ENTRY_LABELS
    for word in candidates:
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", folded):
            results.append((word.title(), True))
    return results


def _clean(label: str) -> str:
    label = re.sub(r"\s*\[[^\]]+\]\s*$", "", label).strip()
    return " ".join(label.split())


def _is_noise(label: str, automation_id: str = "") -> bool:
    haystack = f"{label} {automation_id}".casefold().replace(" ", "")
    return any(marker in haystack for marker in NOISE_MARKERS)


def _humanize(automation_id: str) -> str:
    """Turn an automation id such as SubFilterCombobox into 'Sub Filter'."""
    text = automation_id
    for suffix in ("Button", "ComboBox", "Combobox", "CheckBox", "Checkbox", "TextBlock", "Control"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text.replace("-", " ").replace("_", " "))
    return " ".join(text.split())


def _note_for(label: str) -> str:
    folded = label.casefold()
    for key, note in ACTION_NOTES.items():
        if key in folded:
            return note
    return "Enabled on this screen and reversible as far as the companion can tell."


def _recommend(allowed: list[tuple[str, str]], in_match: bool) -> str:
    """Name one concrete move, so there is always a single thing to do."""
    labels = {label.casefold(): label for label, _ in allowed}
    for key in RECOMMENDATION_ORDER:
        for folded, original in labels.items():
            if key in folded:
                return original
    if allowed:
        return allowed[0][0]
    return "Wait for the client to offer a control" if in_match else "No safe enabled control was readable"
