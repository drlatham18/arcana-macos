from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path


from .actions import PermittedActions, classify_actions, classify_arena_actions, render_actions
from .arena_state import ArenaGameState, describe_phase, render_state
from .knowledge import search_knowledge
from .memory import recall

STOP_WORDS = {
    "a", "about", "am", "an", "and", "are", "can", "do", "does", "for",
    "how", "i", "in", "is", "it", "me", "my", "of", "on", "or", "the",
    "this", "to", "what", "when", "why", "with",
}

TOPICS = {
    "respond": (
        "A response is something you do after a spell or ability has been put on "
        "the stack but before it resolves. You normally need priority. You can pass, "
        "cast an instant or spell with flash, or activate an ability whose timing, "
        "costs, and targets are legal. Passing is not conceding.",
        ["117.1", "117.3", "117.4", "405.5"],
    ),
    "priority": (
        "Priority is the rules permission to take an action. The active player gets "
        "priority first at most points. When every player passes in succession, the "
        "top spell or ability resolves; if the stack is empty, the phase or step ends.",
        ["117.1", "117.3", "117.4"],
    ),
    "stack": (
        "The stack is the waiting area for spells and most activated or triggered "
        "abilities. The newest object is on top and normally resolves first.",
        ["405.1", "405.3", "608.1"],
    ),
    "target": (
        "A target is chosen only when a spell or ability uses the word “target.” "
        "The choice must satisfy every targeting restriction. Arena normally "
        "highlights objects it currently accepts as legal selections.",
        ["115.1", "115.2", "601.2c"],
    ),
    "attack": (
        "During declare attackers, you choose which eligible creatures attack and "
        "what each attacks, then pay any required attack costs. A creature normally "
        "cannot attack if it is tapped or has summoning sickness.",
        ["508.1", "302.6"],
    ),
    "block": (
        "During declare blockers, the defending player assigns eligible untapped "
        "creatures to attackers. Blocking does not tap a creature.",
        ["509.1"],
    ),
    "activate": (
        "An activated ability is written as cost: effect. Activating it means "
        "choosing modes and targets, then paying its costs. Most activated abilities "
        "can be used whenever you have priority unless their text says otherwise.",
        ["602.1", "602.2"],
    ),
    "cast": (
        "Casting a spell includes moving it to the stack, choosing modes and targets, "
        "determining its cost, and paying that cost. Instants may normally be cast "
        "with priority; other card types usually require your main phase, an empty "
        "stack, and priority.",
        ["601.2", "304.5", "307.5"],
    ),
    "resolve": (
        "To resolve a spell or ability, follow its instructions in order. Players do "
        "not receive priority during resolution. State-based actions are checked "
        "afterward before a player gets priority.",
        ["608.1", "608.2", "117.2e"],
    ),
    "mana": (
        "Mana is spent to pay costs. Activating a mana ability normally does not use "
        "the stack and cannot be responded to. Having lands visible does not prove "
        "every colored or additional cost can be paid.",
        ["106.1", "605.3"],
    ),
    "wildcard": (
        "Wildcards craft a card of the matching rarity. Crafting is normally irreversible, so inspect the exact printing and format legality before confirming. Arena's Craft All button can spend several rarities at once.",
        ["wildcard", "craft all", "economy"],
    ),
    "full control": (
        "Full Control makes Arena stop at priority points it normally shortcuts and exposes more manual ordering and mana choices. Turn it on before the interaction you need; it cannot rewind a priority pass.",
        ["full control", "stops", "priority"],
    ),
    "format": (
        "Arena supports rotating, nonrotating, tabletop-parity, digital, Constructed, and Limited formats. Legality and banned or rebalanced cards change over time, so the current client is authoritative.",
        ["formats", "standard", "alchemy", "historic", "timeless", "explorer"],
    ),
    "draft": (
        "Draft is Limited: select cards from rotating packs, then build a deck of at least 40 cards. Arena offers multiple draft queues whose opponents, match structure, entry, and rewards differ.",
        ["draft", "limited", "minimum 40"],
    ),
}


@dataclass(frozen=True)
class OfflineExplanation:
    heading: str
    summary: str
    options: list[tuple[str, str]]
    evidence: str
    caveat: str
    # Filled in for every screen by explain_visible_state, from the real controls
    # rather than from the per-screen guidance above.
    actions: PermittedActions | None = None


QUICK_QUESTIONS = (
    "Review my last match",
    "Explain this screen",
    "What do I click next?",
    "Why is this unavailable?",
    "What am I allowed to do?",
    "Done — review screen",
)



def is_arena_window(window_title: str) -> bool:
    """Arena's window is titled `MTGA`; Magic Online spells its name out."""
    title = window_title.casefold()
    return "mtga" in title or "gathering arena" in title


def screen_text_matches_client(window_title: str, screen_text: str, arena: ArenaGameState | None = None) -> bool:
    """Reject desktop/overlap captures before they become advice or memory."""
    title = window_title.casefold()
    text = screen_text.casefold()
    foreign_markers = (
        "android studio", "gradle", "sdk tools", "developer account", "google play console",
        "mtg rules companion", "what do i click next", "teach / correct me",
    )
    if any(marker in text for marker in foreign_markers):
        return False
    if is_arena_window(window_title):
        # Arena is a Unity client. Its board renders almost no OCR-readable
        # text, so an almost empty frame is normal rather than a bad capture,
        # and the old minimum-length rule silently threw every board away.
        # Nothing foreign is on screen, so accept it and let the detailed log
        # carry the evidence.
        return True
    if "magic: the gathering online" in title:
        markers = (
            "collection", "constructed", "limited", "store", "trade", "online",
            "log in", "forgot password", "event", "change deck", "game details",
        )
        # The main MTGO shell always exposes several of these. Separate match
        # windows use a different title and are accepted below.
        return sum(marker in text for marker in markers) >= 2
    return len(screen_text.strip()) >= 12


def explain_visible_state(
    window_title: str,
    screen_text: str,
    log_text: str = "",
    controls: object = None,
    arena: ArenaGameState | None = None,
) -> OfflineExplanation:
    """Explain the screen and attach what it actually permits right now.

    The per-screen branches below supply expert guidance for layouts the
    companion recognizes. The permitted-action list is computed for every
    screen, recognized or not, so an unmapped page still reports real controls.

    Arena is handled separately and first: its board is unreadable by OCR and
    invisible to UI Automation, so its own detailed log is the only accurate
    source and is authoritative whenever a game is in progress.
    """
    if arena is not None and arena.in_match:
        return replace(_explain_arena(arena), actions=classify_arena_actions(arena))
    if arena is not None and is_arena_window(window_title):
        # Outside a game Arena still shows nothing an OCR classifier written for
        # MTGO can read, so describe the client honestly instead of guessing.
        return replace(_explain_arena_menu(arena), actions=classify_arena_actions(arena))
    state = _classify_screen(window_title, screen_text, log_text)
    permitted = classify_actions(window_title, f"{screen_text}\n{log_text}", controls)
    return replace(state, actions=permitted)


def _arena_log_note(arena: ArenaGameState) -> str:
    """Say why the Arena board is or is not readable, in one sentence."""
    if not arena.available or not arena.detailed:
        return arena.reason
    if arena.game_over and arena.result:
        return f"The last game is finished. {arena.result}"
    return "Arena's detailed log is being read; it will show the full board as soon as a game starts."


def _explain_arena_menu(state: ArenaGameState) -> OfflineExplanation:
    """Arena outside a game. Nothing here is readable, so say what is true."""
    if not state.detailed:
        return OfflineExplanation(
            "The Arena game feed is not ready",
            state.reason,
            [
                ("Open the gear icon, top-right", "Then choose View Account."),
                ("Tick Detailed Logs (Plugin Support)", "This is what lets me read the board without OCR."),
                ("Restart Arena", "The setting only takes effect on a fresh client start."),
            ],
            state.reason,
            "Arena renders its board with Unity, so OCR alone reads almost nothing. The detailed log is the fix.",
        )
    finished = f" {state.result}" if state.result else ""
    return OfflineExplanation(
        "No game is in progress in the recorded log",
        f"I am reading Arena's detailed log and will describe the whole board the moment a game starts.{finished}",
        [
            ("Start a game with Play", "Choose a mode whose entry cost you have already read."),
            ("Ask a rules question here", "The bundled Comprehensive Rules and Arena handbook are searchable offline."),
            ("Leave this running", "I re-read the log every two seconds and will explain the board on its own."),
        ],
        state.summary,
        "Menus and the store are not read from the log. This Mac edition does not capture the screen.",
    )


def _explain_arena(state: ArenaGameState) -> OfflineExplanation:
    """Describe a live Arena game from the client's own rules-engine records."""
    whose = "your turn" if state.my_turn else "your opponent's turn"
    readable_phase = describe_phase(state.phase, state.step)
    heading = f"Arena match — turn {state.turn_number or '?'}, {whose}"
    if readable_phase:
        heading += f", {readable_phase}"

    them = state.opponent_name or "your opponent"
    lines = [f"Life totals: you {state.my_life}, {them} {state.opponent_life}."]
    if state.request:
        ask = f"{state.request} — {state.prompt}" if state.prompt else state.request
        lines.append(f"Arena is asking you to: {ask}")
    elif state.waiting_on_me:
        lines.append("Arena is holding priority for you; nothing resolves until you act.")
    else:
        lines.append(f"The recorded game state is waiting on {them}.")
    if state.stack:
        lines.append("On the stack: " + ", ".join(card.describe() for card in state.stack) + ".")
    attackers = [c for c in state.opponent_battlefield if c.attacking]
    if attackers:
        incoming = sum(c.power or 0 for c in attackers)
        lines.append(
            f"{them} is attacking with {len(attackers)} creature(s) for {incoming} total power: "
            + ", ".join(c.describe() for c in attackers)
            + "."
        )
    summary = " ".join(lines)

    options: list[tuple[str, str]] = [
        (action.label(), action.detail or "Arena's rules engine currently accepts this action.")
        for action in state.actions[:6]
    ]
    if not options:
        options = [(
            "No castable or activatable action is available",
            "Arena's engine currently offers you no spell or ability, so the only move is to answer the prompt.",
        )]
    if state.hand:
        options.append((
            f"Your hand: {', '.join(card.name or 'unknown card' for card in state.hand)}",
            "Read from Arena's log, so hidden information stays hidden: this is your hand only.",
        ))

    return OfflineExplanation(
        heading,
        summary,
        options,
        render_state(state),
        "Read from Arena's own detailed log, so the board is exact. It cannot see cosmetic overlays, "
        "emotes, or anything the client has not yet reported.",
    )


def _arena_steps(state: ArenaGameState) -> str:
    """One exact, click-by-click answer to the request Arena is making."""
    if not state.available or not state.detailed:
        return (
            f"{state.reason}\n\n"
            "1. Open Arena and click the gear icon in the top-right.\n"
            "2. Choose `View Account`.\n"
            "3. Tick `Detailed Logs (Plugin Support)`.\n"
            "4. Restart Arena. I can then read the whole board without OCR."
        )
    if not state.in_match:
        finished = f" {state.result}" if state.result else ""
        return (
            f"No Arena game is in progress right now.{finished}\n\n"
            "1. Click `Play` on the Arena home screen.\n"
            "2. Choose a mode whose entry cost you have already read; do not spend gems or gold unless you meant to.\n"
            "3. Start the game, then ask me again. I will read the board from Arena's own log."
        )
    header = f"Turn {state.turn_number or '?'}, {'your turn' if state.my_turn else 'the opponent’s turn'}."
    if not state.waiting_on_me:
        return (
            f"{header} Arena is waiting on your opponent, so there is nothing to click.\n\n"
            "1. Do not click the board; input is not accepted at this moment.\n"
            "2. Wait for the prompt bar to light up.\n"
            "3. Ask me again once it does and I will read the new request."
        )
    request = state.request
    playable = [action.label() for action in state.actions[:4]]
    if request == "Declare blockers":
        attackers = [c for c in state.opponent_battlefield if c.attacking]
        blockers = [c for c in state.my_battlefield if not c.tapped and c.is_creature]
        incoming = sum(c.power or 0 for c in attackers)
        return (
            f"{header} Arena is asking you to declare blockers.\n\n"
            f"1. Incoming attack: {', '.join(c.describe() for c in attackers) or 'none readable'} — "
            f"{incoming} total power against your {state.my_life} life.\n"
            f"2. Untapped creatures to consider for blocking: {', '.join(c.describe() for c in blockers) or 'none'}.\n"
            "3. Drag each blocker onto the attacker it should block. Blocking does not tap the blocker.\n"
            "4. Click the confirm button. Confirming with nothing assigned lets every attacker through."
        )
    if request == "Declare attackers":
        eligible = [c for c in state.my_battlefield if c.is_creature and not c.tapped and "defender" not in c.keywords and (not c.summoning_sick or "haste" in c.keywords)]
        return (
            f"{header} Arena is asking you to declare attackers.\n\n"
            f"1. Potential attackers (check restrictions in Arena): {', '.join(c.describe() for c in eligible) or 'none'}.\n"
            f"2. {them_blockers(state)}\n"
            "3. Click each creature you want to attack with, or use `All Attack`.\n"
            "4. Confirm. Declaring no attackers is legal and moves the game on."
        )
    if request == "Keep or mulligan":
        return (
            f"{header} Arena is asking whether to keep this opening hand.\n\n"
            f"1. Your hand: {', '.join(card.name or 'unknown card' for card in state.hand) or 'not readable yet'}.\n"
            "2. `Keep` accepts it; after a mulligan you then put the required number of cards on the bottom.\n"
            "3. `Mulligan` shuffles back and draws seven again.\n"
            "4. This is a judgement call about hidden information, so it is your decision, not mine."
        )
    if request == "Pay a cost":
        return (
            f"{header} Arena needs a cost paid before the spell or ability continues.\n\n"
            f"1. You control {state.mana_sources_available} untapped mana source(s).\n"
            "2. Accept Arena's auto-tap proposal, or click your own sources to choose which ones tap.\n"
            "3. `Cancel` is available while the payment is unfinished and backs the cast out."
        )
    if playable:
        lines = [f"{header} You have priority, so Arena lists these timing options; confirm costs and targets.", ""]
        lines.extend(f"{index}. {label}" for index, label in enumerate(playable, start=1))
        lines.append(f"{len(playable) + 1}. Or pass priority (the resolve button, or Space) to let the game continue.")
        lines.append(f"You control {state.mana_sources_available} untapped mana source(s).")
        return "\n".join(lines)
    return (
        f"{header} Arena is waiting on you, but its engine offers no castable spell or activatable ability.\n\n"
        "1. Answer the prompt Arena is showing at the bottom of the screen.\n"
        "2. Passing priority is not conceding; it just lets the game continue.\n"
        f"3. For reference, you control {state.mana_sources_available} untapped mana source(s)."
    )


def them_blockers(state: ArenaGameState) -> str:
    untapped = [c for c in state.opponent_battlefield if not c.tapped and c.power is not None]
    if not untapped:
        return "Your opponent controls no untapped creature that could block."
    return "Possible blockers: " + ", ".join(c.describe() for c in untapped) + "."


def _classify_screen(window_title: str, screen_text: str, log_text: str = "") -> OfflineExplanation:
    combined_evidence = f"{screen_text}\n{log_text}".strip()
    text = f"{window_title}\n{combined_evidence}".casefold()
    lobby_words = (
        "home", "decks", "packs", "store", "profile", "mastery",
        "log in", "forgot password", "create new account", "disconnected",
    )
    match_words = ("opponent", "your turn", "next", "resolve", "attack", "block", "mulligan", "keep 7")
    if "quick play match options" in text:
        return OfflineExplanation(
            "Quick Play Match Options popup detected",
            "This dialog configures a no-event-entry Quick Play game. It exposes the selected deck, format, player-count, queue-type, Play, and Cancel controls.",
            [
                ("Select format first", "Open the SELECT FORMAT dropdown and choose the format matching the deck."),
                ("Choose player count", "Use 1 for Solitaire practice or 2 for a normal one-on-one game."),
                ("Play only after checking settings", "Play starts the configured game; Cancel closes the dialog."),
            ],
            _evidence(combined_evidence, ("quick play", "main deck", "sideboard", "select format", "number of players", "queue type", "play", "cancel")),
            "The companion will not press Play for you. Confirm the format and player count yourself before starting.",
        )
    if "add to cart" in text and "subtotal" in text and "checkout" in text:
        return OfflineExplanation(
            "MTGO Store screen detected",
            "This page sells tickets, boosters, bundles, and other products. The cart and subtotal are shown on the right.",
            [
                ("Leave the cart empty", "Do not press any Add to Cart button during guided mapping."),
                ("Do not use Checkout", "Checkout can charge real money after products are added."),
                ("Return with HOME", "HOME in the upper-left safely leaves the Store."),
            ],
            _evidence(combined_evidence, ("add to cart", "subtotal", "checkout", "redeem", "event ticket")),
            "Product names and prices change. The companion never adds products, redeems codes, or checks out.",
        )
    if "classifieds" in text and "all current posts" in text and "my post" in text:
        return OfflineExplanation(
            "MTGO Trade Classifieds screen detected",
            "This page lists player and bot trade advertisements. Opening a post can begin a trade workflow.",
            [
                ("Do not open a trader", "No classified post needs to be selected for screen mapping."),
                ("Do not submit a post", "Submit, Remove Post, and Cancel Changes alter your classified listing."),
                ("Return with HOME", "HOME in the upper-left safely leaves Trade."),
            ],
            _evidence(combined_evidence, ("classifieds", "current posts", "previous trade", "my post", "submit")),
            "The companion never opens, confirms, or submits a trade and never posts a classified message.",
        )
    if "limited" in text and "choose entry option" in text and any(
        word in text for word in ("event tickets", "play points", "entry token", "boosters")
    ):
        return OfflineExplanation(
            "MTGO Limited event-entry screen detected",
            "A Draft or Sealed event is selected and the center panel is offering paid entry methods.",
            [
                ("Do not click BUY", "BUY may spend Event Tickets, Play Points, boosters, or an entry token."),
                ("Read More Info safely", "More Info and View Prizes are informational, but event details can change."),
                ("Return with HOME", "HOME safely leaves the selected Limited event."),
            ],
            _evidence(combined_evidence, ("draft", "sealed", "entry option", "event tickets", "play points", "buy")),
            "Limited entry methods and prizes are live account data. No entry option is selected automatically.",
        )
    popup_name = _accessible_popup_name(combined_evidence)
    if popup_name:
        buttons = _accessible_button_names(combined_evidence)
        safe_buttons = [name for name in buttons if not _dangerous_label(name)]
        dangerous_buttons = [name for name in buttons if _dangerous_label(name)]
        options = [
            (f"Button: {name}", "This enabled button is exposed by the open dialog.")
            for name in safe_buttons[:5]
        ]
        if dangerous_buttons:
            options.append(
                ("Protected controls", "Do not press: " + ", ".join(dangerous_buttons[:5]) + ".")
            )
        return OfflineExplanation(
            f"Open MTGO popup: {popup_name}",
            f"MTGO has an open dialog named {popup_name}. The companion read its controls directly, without relying on the pixels behind it.",
            options or [("Close with Esc", "No named enabled button was readable; Esc is the safest reversible exit.")],
            _evidence(combined_evidence, ("open popup", "button:", "combobox:", "listitem:", "checkbox:")),
            "The companion lists controls but will not choose a consequential answer, purchase, trade, delete, or concede action for you.",
        )
    if (
        "online" in window_title.casefold()
        and "change" in text
        and "deck" in text
        and any(word in text for word in ("event tickets", "play points", "entries end", "entry closes"))
    ):
        return OfflineExplanation(
            "MTGO Constructed Events screen detected",
            "A Constructed event is selected. The center panel shows its deck and entry choices; clicking BUY or an entry option may spend Event Tickets or Play Points.",
            [
                ("Do not click BUY", "The visible selection is asking for an event-entry payment."),
                ("Use the far-left format list", "Scroll the format list downward to locate a no-entry-fee practice or Freeform option."),
                ("Ask again when its label is visible", "Press What do I click next? so the companion verifies the exact label before you enter."),
            ],
            _evidence(combined_evidence, ("league", "deck", "ticket", "play point", "entry", "standard", "legacy", "modern")),
            "This is an event-entry screen, not an active game. Entry structures and prices are controlled by MTGO.",
        )
    if _contains_any_phrase(text, ("mulligan",)) and _contains_any_phrase(text, ("keep",)):
        return OfflineExplanation(
            "Opening hand: keep or mulligan",
            "The match is asking whether to keep this opening hand. Each mulligan draws a fresh seven and puts one card on the bottom when you keep.",
            [
                ("Keep", "Accept this hand. If you have already mulliganed, put the required number of cards on the bottom."),
                ("Mulligan", "Shuffle back and draw seven again. The bottom-card cost grows with each mulligan."),
            ],
            _evidence(combined_evidence, ("mulligan", "keep", "hand", "london")),
            "This is a judgement call about hidden information. The companion states the cost of each choice and does not choose for you.",
        )
    if _contains_any_phrase(text, ("no response", "take action", "yield")) or (
        "online" in window_title.casefold() and _contains_any_phrase(text, ("next turn",))
    ):
        return OfflineExplanation(
            "MTGO priority stop",
            "MTGO is holding priority and waiting for you. Nothing resolves until every player passes in succession.",
            [
                ("No Response / Yield", "Pass priority. The top object on the stack then resolves. This is not conceding."),
                ("Take Action", "Open the responses MTGO currently accepts: instants, flash spells, and legal activated abilities."),
                ("Next Turn", "Skip your remaining stops this turn. F6 does the same thing."),
            ],
            _evidence(combined_evidence, ("no response", "take action", "yield", "next turn", "priority", "stack", "step")),
            "MTGO's own highlighting decides what is castable. The companion will not pass priority or act for you.",
        )
    if "online" in window_title.casefold() and _contains_any_phrase(
        text, ("pay", "mana", "mana cost", "payment")
    ) and any(word in text for word in ("cast", "activate", "spell", "ability")):
        return OfflineExplanation(
            "MTGO is asking for mana payment",
            "A spell or ability is on the stack and MTGO needs its cost paid. Click your own untapped lands or mana sources to produce each required symbol.",
            [
                ("Click an untapped land", "Each click taps that source for its mana. Match the symbols shown in the cost."),
                ("Cancel", "Available while the payment is unfinished; it backs the cast out."),
            ],
            _evidence(combined_evidence, ("pay", "mana", "cost", "cancel", "cast")),
            "Mana payment inside a match spends no account currency. The companion never clicks lands for you.",
        )
    if any(word in text for word in lobby_words) and not any(word in text for word in match_words):
        if any(word in text for word in ("log in", "forgot password", "create new account")):
            menu_options = [
                ("Click the account-name box", "It is in the center of the MTGO login panel."),
                ("Click the password box below it", "Enter the password, then click LOG IN once."),
            ]
        elif "online" in window_title.casefold():
            menu_options = [
                ("Click CONSTRUCTED", "It is in the top bar between COLLECTION and LIMITED."),
                ("Avoid VIEW EVENT", "The large Home-page promotion may lead to a specialized or paid event."),
            ]
        else:
            menu_options = [
                ("Click Play", "Use the Play control on the Arena Home screen."),
                ("Choose a free queue", "Do not confirm a paid event entry until its cost and format are clear."),
            ]
        return OfflineExplanation(
            "Magic client menu detected",
            "The client appears to be outside a match. You can ask about formats, decks, events, settings, or rules here.",
            menu_options,
            _evidence(combined_evidence, lobby_words) or window_title,
            "Menus do not expose a live stack, priority, or selectable game objects.",
        )
    if any(word in text for word in ("choose target", "select target", "target up to", "choose a target")):
        return OfflineExplanation(
            "The game is asking for a target",
            "A spell or ability is being cast or activated and needs a legal target before it can continue.",
            [("Select a highlighted object", "Highlighted objects are the choices the client currently accepts."),
             ("Cancel", "Available only if the client shows a Cancel button and the action is still reversible.")],
            _evidence(combined_evidence, ("target", "select", "choose")),
            "OCR cannot prove every restriction. The exact card text controls what is legal.",
        )
    if "declare attackers" in text or "choose attackers" in text:
        return OfflineExplanation(
            "Declare attackers",
            "You are in the turn-based action where attacking creatures are chosen.",
            [("Choose eligible attackers", "Untapped creatures you have controlled since your turn began are normally eligible."),
             ("Attack with none / Done", "You may normally declare no attackers and continue.")],
            _evidence(combined_evidence, ("attack", "done")),
            "Effects can add restrictions, requirements, or attack costs that are not readable here.",
        )
    if "declare blockers" in text or "choose blockers" in text:
        return OfflineExplanation(
            "Declare blockers",
            "You are choosing how eligible creatures block attacking creatures.",
            [("Assign blockers", "Choose legal attacker/blocker pairings accepted by Arena."),
             ("Block with none / Done", "You may normally decline to block and continue.")],
            _evidence(combined_evidence, ("block", "done")),
            "Evasion abilities and blocking restrictions may change which assignments are legal.",
        )
    if _contains_any_phrase(text, ("yes", "no", "ok", "pass priority", "yield")):
        return OfflineExplanation(
            "A prompt or priority stop is visible",
            "Arena is waiting for your input before the game can continue.",
            [("Pass / OK", "Pass priority or confirm the current prompt; this is not conceding."),
             ("Cast at instant speed", "Possible only if an enabled card has legal timing, targets, and payable costs."),
             ("Activate an ability", "Possible only if Arena exposes an enabled ability and its costs and targets are legal.")],
            _evidence(combined_evidence, ("yes", "no", "ok", "pass", "yield", "phase", "step", "priority")),
            "The screen text alone may not identify every enabled card or ability. Arena's highlighting is authoritative.",
        )
    return OfflineExplanation(
        "Active match detected",
        "The match window is active, but no specific readable prompt was detected in this frame.",
        [("Use enabled Arena controls", "Bright or highlighted controls are actions the client currently permits."),
         ("Wait for a prompt or priority stop", "The companion will rescan when the screen changes."),
         ("Ask a rules question", "Use the chat tab to search the bundled Comprehensive Rules.")],
        _evidence(combined_evidence, ("phase", "turn", "step", "priority", "prompt")),
        "Card art and small rules text may not be readable by offline OCR. This mode will not guess.",
    )


def answer_question(
    question: str,
    window_title: str,
    screen_text: str,
    log_text: str = "",
    arena: ArenaGameState | None = None,
) -> str:
    quick = _answer_quick_question(question, window_title, screen_text, log_text, arena)
    if quick:
        return quick
    words = [w for w in re.findall(r"[a-z0-9.]+", question.casefold()) if w not in STOP_WORDS]
    topic_key = next((key for key in TOPICS if key in question.casefold()), None)
    if topic_key:
        summary, rule_ids = TOPICS[topic_key]
        excerpts = search_rules(rule_ids + words, limit=3)
        return _format_answer(summary, excerpts, window_title, screen_text, log_text, question)
    excerpts = search_rules(words, limit=4)
    if not excerpts:
        state = explain_visible_state(window_title, screen_text, log_text, arena=arena)
        memories = recall(f"{question} {screen_text}", limit=2)
        learned = ""
        if memories:
            learned = "\n\nThings you taught me locally:\n" + "\n".join(f"• {item.note}" for item in memories)
        return f"{state.summary}{learned}\n\nI couldn't find a close official passage for that wording. You can rephrase it, or use Teach / correct me if this is an Arena screen behavior I should remember."
    return _format_answer("Here are the closest relevant passages from the local knowledge library:", excerpts, window_title, screen_text, log_text, question)


def _answer_quick_question(
    question: str,
    window_title: str,
    screen_text: str,
    log_text: str,
    arena: ArenaGameState | None = None,
) -> str:
    normalized = re.sub(r"[^a-z ]", "", question.casefold()).strip()
    if normalized in {
        "review my last match",
        "review my last game",
        "review the last match",
        "how did i play",
    }:
        # Imported here: the reviewer attaches itself to the shared Arena log
        # reader, and importing it at module load would do that as a side
        # effect of merely asking a rules question.
        from .match_review import render_last_match  # noqa: PLC0415

        return render_last_match()
    state = explain_visible_state(window_title, screen_text, log_text, arena=arena)
    exact_steps = _screen_specific_steps(window_title, screen_text, state, arena)
    if normalized == "how do i start":
        return exact_steps
    if normalized in {"what am i allowed to do", "what can i do", "what are my options"}:
        return f"{state.heading}\n\n{render_actions(state.actions)}"
    if normalized == "explain this screen":
        evidence = state.evidence or "No useful prompt text was readable."
        return (
            f"{state.heading}\n\n{state.summary}\n\nExactly what to do:\n{exact_steps}\n\n"
            f"{render_actions(state.actions)}\n\nWhat I could read:\n{evidence}\n\nLimit: {state.caveat}"
        )
    if normalized in {"what do i click next", "done  review screen", "done review screen"}:
        return exact_steps
    if normalized == "why is this unavailable":
        return (
            "Common causes include timing restrictions, insufficient or wrong-colored mana, "
            "additional costs, or no legal target. Arcana does not read tooltips from the screen. "
            "Check the card’s tooltip in Arena and include its text in your question."
        )
    return ""


def _screen_specific_steps(
    window_title: str,
    screen_text: str,
    state: OfflineExplanation,
    arena: ArenaGameState | None = None,
) -> str:
    text = screen_text.casefold()
    is_mtgo = "online" in window_title.casefold()
    # Arena publishes its exact request and legal actions, so its own log beats
    # every pixel heuristic below.
    if arena is not None and is_arena_window(window_title):
        return _arena_steps(arena)
    if "quick play match options" in text:
        deck_name = _quick_play_deck_name(screen_text)
        deck_detail = (
            f"The selected deck is `{deck_name}`; the dialog reports `MAIN DECK (60)` and `SIDEBOARD (15)`."
            if deck_name
            else "Read the selected deck name directly above `MAIN DECK (60)` and `SIDEBOARD (15)`."
        )
        suggested_format = "Legacy" if deck_name and "legacy" in deck_name.casefold() else "the format that matches that deck"
        return (
            "You are in the `Quick Play Match Options` popup.\n\n"
            f"1. Check the deck on the left. {deck_detail}\n"
            f"2. Open the `SELECT FORMAT` dropdown on the upper-right side and choose `{suggested_format}`.\n"
            "3. Under `NUMBER OF PLAYERS`, choose `1` for Solitaire practice or `2` for a normal one-on-one game.\n"
            "4. Leave the queue type at its default for your first practice attempt.\n"
            "5. Click `Play` only after those values are correct. Click `Cancel` to leave without starting anything."
        )
    if "add to cart" in text and "subtotal" in text and "checkout" in text:
        return (
            "You are in the MTGO Store.\n\n"
            "1. Do not click `ADD TO CART`, `Redeem`, `Update Shipping Address`, or `CHECKOUT`.\n"
            "2. Verify the cart on the right remains empty and the subtotal remains `$0.00`.\n"
            "3. Click `HOME` in the upper-left to leave without purchasing anything."
        )
    if "classifieds" in text and "all current posts" in text and "my post" in text:
        return (
            "You are in Trade → Classifieds.\n\n"
            "1. Do not double-click a trader or bot listing; that can begin a trade.\n"
            "2. Do not type in `My Post: Message` or press `Submit`, `Remove Post`, or `Cancel Changes`.\n"
            "3. Click `HOME` in the upper-left to leave the Trade page safely."
        )
    if "limited" in text and "choose entry option" in text and any(
        word in text for word in ("event tickets", "play points", "entry token", "boosters")
    ):
        return (
            "You are on a Limited event-entry page.\n\n"
            "1. Do not click either `BUY` control or any Event Ticket, booster, Play Point, or entry-token option.\n"
            "2. `More Info` and `View Prizes` are informational, but no further click is required for mapping.\n"
            "3. Click `HOME` in the upper-left to leave without entering the event."
        )
    popup_name = _accessible_popup_name(screen_text)
    if popup_name:
        buttons = _accessible_button_names(screen_text)
        safe = [name for name in buttons if not _dangerous_label(name)]
        blocked = [name for name in buttons if _dangerous_label(name)]
        lines = [f"You are in the `{popup_name}` popup.", ""]
        if safe:
            lines.append("Enabled named buttons I can read: " + ", ".join(f"`{name}`" for name in safe[:8]) + ".")
        if blocked:
            lines.append("Do not click these consequential controls: " + ", ".join(f"`{name}`" for name in blocked[:8]) + ".")
        lines.extend([
            "1. Read the popup title and its message from top to bottom.",
            "2. Use only a named reversible button such as `Cancel`, `Close`, or `Back` if you want to leave it.",
            "3. If the popup asks for payment, trade confirmation, deletion, or concession, stop; the companion will not proceed.",
        ])
        return "\n".join(lines)
    if any(word in text for word in ("log in", "forgot password", "create new account")):
        return (
            "You are on the login screen.\n\n"
            "1. Click the account-name box in the center of the MTGO window.\n"
            "2. Type your account name.\n"
            "3. Click the password box directly below it and type your password.\n"
            "4. Click `LOG IN`.\n"
            "5. Wait for the Home screen; then press `What do I click next?` here."
        )
    mtgo_home = is_mtgo and all(word in text for word in ("collection", "constructed", "limited", "store", "trade"))
    mtgo_paid_event = (
        is_mtgo
        and "change" in text
        and "deck" in text
        and any(word in text for word in ("event tickets", "play points", "entries end", "entry closes"))
    )
    if mtgo_paid_event:
        format_names = ("Standard", "Pioneer", "Modern", "Legacy", "Vintage", "Pauper", "Commander")
        selected = max(format_names, key=lambda name: text.count(name.casefold()))
        if text.count(selected.casefold()) == 0:
            selected = "a Constructed format"
        return (
            f"You are on the Constructed Events page, and {selected} content is visible. The selected panel is asking for an entry payment.\n\n"
            "1. Do not click the purple `BUY` button or either entry-payment box.\n"
            "2. Do not click `CHANGE DECK` yet.\n"
            "3. If your goal is a no-entry-fee practice game, scroll the far-left format list downward and look for `Freeform` or a practice-room option.\n"
            "4. As soon as that label is visible, press `Explain this screen` again. I will verify the exact label before telling you to enter it."
        )
    if mtgo_home:
        return (
            "1. Click `CONSTRUCTED` in the top navigation bar. It is between `COLLECTION` and `LIMITED`.\n"
            "2. Do not click the large `VIEW EVENT` advertisement; it can lead to a specialized or paid event.\n"
            "3. Wait for the Constructed page to finish loading.\n"
            "4. Return here and press `What do I click next?` again. I will read the new page and give you the next exact click."
        )
    if is_mtgo and "tournament practice" in text:
        return (
            "1. Click `TOURNAMENT PRACTICE`. This is the practice room, not a paid league.\n"
            "2. On the next screen, look for `CREATE` or `JOIN` but do not confirm yet.\n"
            "3. Press `Explain this screen` here so I can tell you which visible game entry to use."
        )
    if is_mtgo and "collection" in text and any(word in text for word in ("decks", "new deck", "quantity")):
        return (
            "1. Find the deck list or deck panel on the left side.\n"
            "2. Single-click the deck you want to use; do not trade or delete anything.\n"
            "3. If no deck exists, click `NEW DECK` once.\n"
            "4. Press `Explain this screen` here after the deck editor opens."
        )
    if "choose target" in text or "select target" in text:
        return (
            "1. Click one object that the game outlines or highlights as legal.\n"
            "2. Check that the selected object now has a stronger outline.\n"
            "3. Click the visible `OK`, `SUBMIT`, or `DONE` button once.\n"
            "4. If nothing is highlighted, click `CANCEL` if it is visible and ask `Why is this unavailable?` again."
        )
    if "declare attackers" in text or "choose attackers" in text:
        return (
            "1. Click each creature you want to attack with; selected attackers should tilt, glow, or move forward.\n"
            "2. To attack with none, leave every creature unselected.\n"
            "3. Click `DONE` or `ATTACK` once to confirm."
        )
    if "declare blockers" in text or "choose blockers" in text:
        return (
            "1. Click or drag an untapped creature you control onto the attacker it should block.\n"
            "2. Confirm the game draws an assignment line or otherwise marks the pairing.\n"
            "3. Repeat for any other blockers, then click `DONE`."
        )
    if _contains_any_phrase(text, ("yes", "no", "ok", "submit", "done")):
        visible = [label.upper() for label in ("yes", "no", "ok", "submit", "done") if _contains_any_phrase(text, (label,))]
        return (
            f"The visible confirmation buttons appear to be: {', '.join(visible)}. Do not choose one until you read the prompt immediately above them.\n"
            "1. Read the prompt from left to right.\n"
            "2. If it asks you to confirm a selection you already made, click `OK`, `SUBMIT`, or `DONE` once.\n"
            "3. If it offers `YES` and `NO`, press `Explain this screen` here first so I can identify the question."
        )
    # Nothing matched a mapped layout, but the permitted-action scan may still
    # have found real enabled controls on this unmapped screen.
    permitted = state.actions
    if permitted is not None and permitted.allowed:
        lines = [
            "This screen is not one I have mapped, so here is what it actually permits right now "
            f"(read from the {permitted.source}).\n"
        ]
        for index, (label, note) in enumerate(permitted.allowed[:5], start=1):
            lines.append(f"{index}. `{label}` — {note}")
        if permitted.blocked:
            lines.append(
                "\nDo not click: "
                + ", ".join(f"`{label}`" for label, _ in permitted.blocked)
                + ". These can spend currency or destroy something."
            )
        lines.append(f"\nIf you want one choice: `{permitted.recommended}`.")
        return "\n".join(lines)
    return (
        "I cannot identify one safe exact click from the readable text in this frame. Do not click a purchase, entry, trade, concede, or delete button.\n"
        "1. Close any card preview or tooltip with the `Esc` key once.\n"
        "2. Wait one second for the screen to settle.\n"
        "3. Press `Explain this screen` here again so I can read a clean frame."
    )


def search_rules(terms: list[str], limit: int = 4) -> list[str]:
    return [f"{hit.text[:650]} [{hit.citation}]" for hit in search_knowledge(terms, limit)]


def _format_answer(summary: str, excerpts: list[str], window_title: str, screen_text: str, log_text: str = "", question: str = "") -> str:
    visible = _evidence(
        f"{screen_text}\n{log_text}",
        ("priority", "target", "attack", "block", "cast", "activate", "ok", "pass", "phase", "step", "prompt"),
    )
    parts = [summary]
    if visible:
        parts.append(f"Visible Arena text: {visible}")
    if excerpts:
        parts.append("Relevant local rules:\n" + "\n\n".join(f"• {item}" for item in excerpts))
    memories = recall(f"{question} {screen_text}", limit=2)
    if memories:
        parts.append("Things you taught me locally:\n" + "\n".join(f"• {item.note}" for item in memories))
    parts.append("This answer uses local rules and any available Arena log data. Hidden cards are not inferred.")
    return "\n\n".join(parts)


def _evidence(text: str, needles: tuple[str, ...]) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    matches = [line for line in lines if any(needle in line.casefold() for needle in needles)]
    return " · ".join(matches[:4])[:400]


def _contains_any_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) for phrase in phrases)


def _accessible_popup_name(text: str) -> str:
    match = re.search(r"(?im)^Open popup:\s*(.+?)\s*$", text)
    return match.group(1).strip() if match else ""


def _accessible_button_names(text: str) -> list[str]:
    """Named buttons the dialog currently accepts. Disabled ones are dropped so
    they are never offered as something the user can press."""
    names: list[str] = []
    for match in re.finditer(r"(?im)^Button:\s*(.+?)\s*$", text):
        raw = match.group(1)
        if re.search(r"\(disabled\)\s*$", raw, re.IGNORECASE):
            continue
        name = re.sub(r"\s+\[[^\]]+\]\s*$", "", raw).strip()
        if name and name.casefold() not in {item.casefold() for item in names}:
            names.append(name)
    return names


def _dangerous_label(label: str) -> bool:
    folded = label.casefold()
    return any(
        word in folded
        for word in (
            "buy", "purchase", "redeem", "event ticket", "play point", "trade",
            "delete", "concede", "pay", "checkout", "confirm", "submit",
        )
    )


def _quick_play_deck_name(text: str) -> str:
    match = re.search(r"(?im)^Text:\s*Change Deck\s*\nText:\s*(.+?)\s*$", text)
    return match.group(1).strip() if match else ""
