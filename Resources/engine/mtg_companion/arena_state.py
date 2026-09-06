"""Read the live Arena game state from the client's own Player.log.

Arena is a Unity client: it publishes no UI Automation tree, and OCR reads
almost nothing off its dark, stylised board. What it does publish, when
Detailed Logs are on, is the complete Game Rules Engine conversation --
turn, phase, step, priority, every visible card, and the exact list of actions
the engine will accept from you right now.

This module tails that log incrementally, rebuilds the current game state from
the last full snapshot plus every diff after it, and resolves grpIds to real
card names through the client's own card database.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .arena_cards import load_card_database

# How far back to read when the companion starts in the middle of a game.
INITIAL_TAIL_BYTES = 4_000_000

PHASE_NAMES = {
    "Phase_Beginning": "beginning phase",
    "Phase_Main1": "precombat main phase",
    "Phase_Combat": "combat phase",
    "Phase_Main2": "postcombat main phase",
    "Phase_Ending": "ending phase",
}
STEP_NAMES = {
    "Step_Untap": "untap step",
    "Step_Upkeep": "upkeep step",
    "Step_Draw": "draw step",
    "Step_BeginCombat": "beginning of combat step",
    "Step_DeclareAttack": "declare attackers step",
    "Step_DeclareBlock": "declare blockers step",
    "Step_FirstStrikeDamage": "first-strike combat damage step",
    "Step_CombatDamage": "combat damage step",
    "Step_EndCombat": "end of combat step",
    "Step_End": "end step",
    "Step_Cleanup": "cleanup step",
}
MANA_SYMBOLS = {
    "ManaColor_White": "W",
    "ManaColor_Blue": "U",
    "ManaColor_Black": "B",
    "ManaColor_Red": "R",
    "ManaColor_Green": "G",
    "ManaColor_Colorless": "C",
}
# What each engine request is actually asking you for, in plain words.
REQUEST_LABELS = {
    "GREMessageType_ActionsAvailableReq": "You have priority",
    "GREMessageType_DeclareAttackersReq": "Declare attackers",
    "GREMessageType_DeclareBlockersReq": "Declare blockers",
    "GREMessageType_AssignDamageReq": "Assign combat damage",
    "GREMessageType_MulliganReq": "Keep or mulligan",
    "GREMessageType_PayCostsReq": "Pay a cost",
    "GREMessageType_SelectNReq": "Choose from a list",
    "GREMessageType_SelectTargetsReq": "Choose targets",
    "GREMessageType_SubmitTargetsReq": "Choose targets",
    "GREMessageType_OptionalActionReq": "Answer an optional prompt",
    "GREMessageType_OrderReq": "Put these in an order",
    "GREMessageType_OrderCombatDamageReq": "Order blockers for damage",
    "GREMessageType_SearchReq": "Search a zone",
    "GREMessageType_GroupReq": "Distribute or divide",
    "GREMessageType_IntermissionReq": "The game is over",
    "GREMessageType_ChooseStartingPlayerReq": "Choose who plays first",
}
# Action types that are real decisions. Mana abilities are summarised instead,
# because a normal board floods the list with one entry per untapped land.
MEANINGFUL_ACTIONS = {
    "ActionType_Cast",
    "ActionType_Play",
    "ActionType_Activate",
    "ActionType_Activate_Mana_Or_Ability",
    "ActionType_Special_TurnFaceUp",
    "ActionType_CastLeft",
    "ActionType_CastRight",
    "ActionType_Play_MDFC_Back",
    "ActionType_Cast_MDFC_Back",
}
MANA_ACTIONS = ("ActionType_Activate_Mana", "ActionType_Mana_Ability")


def default_log_directory() -> Path:
    return Path.home() / "Library/Logs/Wizards Of The Coast/MTGA"


def find_player_log() -> Path | None:
    override = os.environ.get("MTGA_PLAYER_LOG")
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() else None
    candidates = [
        default_log_directory() / "Player.log",
        Path.home() / "Library/Logs/Wizards of the Coast/MTGA/Player.log",
        Path.home() / "Library/Logs/com.wizards.mtga/Player.log",
    ]
    for base in (Path.home() / "Library/Application Support/com.wizards.mtga/Logs/Logs",):
        if base.is_dir():
            candidates.extend(base.glob("*.log"))
    existing = []
    for path in candidates:
        try:
            if path.is_file():
                existing.append(path)
        except OSError:
            continue
    return max(existing, key=lambda path: path.stat().st_mtime) if existing else None


# Evergreen keywords that change whether a block or an attack is even legal,
# or whether damage kills. Anything outside this set is treated as unknown text
# and makes the reviewer decline to judge the combat.
COMBAT_KEYWORDS = {
    "flying", "reach", "menace", "deathtouch", "trample", "first strike",
    "double strike", "lifelink", "vigilance", "defender", "indestructible",
    "hexproof", "haste", "flash", "ward",
}


@dataclass(frozen=True)
class ArenaCard:
    instance_id: int
    grp_id: int
    name: str
    type_line: str
    power: int | None = None
    toughness: int | None = None
    damage: int = 0
    tapped: bool = False
    summoning_sick: bool = False
    attacking: bool = False
    blocking: bool = False
    controller: int | None = None
    card_types: tuple[str, ...] = ()
    abilities: tuple[str, ...] = ()
    blocked_attackers: tuple[int, ...] = ()

    @property
    def is_creature(self) -> bool:
        if self.card_types:
            return "creature" in self.card_types
        return "creature" in (self.type_line or "").casefold()

    @property
    def is_land(self) -> bool:
        if self.card_types:
            return "land" in self.card_types
        return "land" in (self.type_line or "").casefold()

    @property
    def keywords(self) -> frozenset[str]:
        """Evergreen keywords this object currently has, per the engine."""
        found = set()
        for text in self.abilities:
            for part in text.split(","):
                word = part.strip().rstrip(".")
                if word in COMBAT_KEYWORDS:
                    found.add(word)
        return frozenset(found)

    @property
    def unreadable_abilities(self) -> bool:
        """True when this object has rules text the reviewer must not reason past."""
        for text in self.abilities:
            words = {part.strip().rstrip(".") for part in text.split(",")}
            if words - COMBAT_KEYWORDS:
                return True
        return False

    @property
    def toughness_left(self) -> int:
        return max(0, (self.toughness or 0) - self.damage)

    def describe(self) -> str:
        parts = [self.name or f"card #{self.instance_id}"]
        if self.power is not None and self.toughness is not None:
            stats = f"{self.power}/{self.toughness}"
            if self.damage:
                stats += f", {self.damage} damage marked"
            parts.append(f"({stats})")
        flags = []
        if self.tapped:
            flags.append("tapped")
        if self.summoning_sick:
            flags.append("summoning sick")
        if self.attacking:
            flags.append("attacking")
        if self.blocking:
            flags.append("blocking")
        if flags:
            parts.append("[" + ", ".join(flags) + "]")
        return " ".join(parts)


@dataclass(frozen=True)
class ArenaAction:
    action_type: str
    card: str
    mana_cost: str
    detail: str

    @property
    def verb(self) -> str:
        raw = self.action_type.replace("ActionType_", "").replace("_", " ")
        return raw.strip() or "Act"

    @property
    def cost_value(self) -> int:
        """Total mana in the rendered cost, e.g. {4}{W}{W} -> 6.

        Arena offers every action whose *timing* is legal, affordable or not:
        it will list a five-drop while you control one land. Anything that
        cares whether you could actually have cast a spell has to do this sum
        itself and compare it against your untapped sources.
        """
        total = 0
        for symbol in re.findall(r"\{([^}]*)\}", self.mana_cost):
            total += int(symbol) if symbol.isdigit() else 1
        return total

    def label(self) -> str:
        text = f"{self.verb} {self.card}".strip()
        if self.mana_cost:
            text += f" for {self.mana_cost}"
        if self.detail:
            text += f" - {self.detail}"
        return text


@dataclass(frozen=True)
class ArenaGameState:
    available: bool = False
    detailed: bool = False
    in_match: bool = False
    reason: str = ""
    my_seat: int | None = None
    my_name: str = ""
    opponent_name: str = ""
    my_life: int | None = None
    opponent_life: int | None = None
    turn_number: int | None = None
    phase: str = ""
    step: str = ""
    active_player: int | None = None
    priority_player: int | None = None
    decision_player: int | None = None
    request: str = ""
    prompt: str = ""
    hand: tuple[ArenaCard, ...] = ()
    my_battlefield: tuple[ArenaCard, ...] = ()
    opponent_battlefield: tuple[ArenaCard, ...] = ()
    stack: tuple[ArenaCard, ...] = ()
    my_graveyard: int = 0
    opponent_graveyard: int = 0
    my_library: int = 0
    opponent_library: int = 0
    opponent_hand: int = 0
    actions: tuple[ArenaAction, ...] = ()
    mana_sources_available: int = 0
    game_over: bool = False
    result: str = ""
    log_path: Path | None = None
    # Identity, so a reviewer can tell one game of a match from the next.
    match_id: str = ""
    game_number: int | None = None
    game_result: str = ""
    game_result_reason: str = ""

    @property
    def my_turn(self) -> bool:
        return self.my_seat is not None and self.active_player == self.my_seat

    @property
    def waiting_on_me(self) -> bool:
        if self.my_seat is None:
            return False
        return self.decision_player == self.my_seat or self.priority_player == self.my_seat

    @property
    def summary(self) -> str:
        """One-line summary, kept for callers that only want a status string."""
        if not self.available or not self.detailed:
            return self.reason
        if not self.in_match:
            return "Arena detailed logs are readable, but no game is in progress."
        bits: list[str] = []
        if self.turn_number:
            whose = "your turn" if self.my_turn else "opponent's turn"
            bits.append(f"turn {self.turn_number} ({whose})")
        readable_phase = describe_phase(self.phase, self.step)
        if readable_phase:
            bits.append(readable_phase)
        if self.my_life is not None and self.opponent_life is not None:
            bits.append(f"life {self.my_life}-{self.opponent_life}")
        if self.request:
            bits.append(self.request.lower())
        return " - ".join(bits)


def describe_phase(phase: str, step: str) -> str:
    if step:
        return STEP_NAMES.get(step, step.replace("Step_", "").replace("_", " ").lower())
    if phase:
        return PHASE_NAMES.get(phase, phase.replace("Phase_", "").replace("_", " ").lower())
    return ""


def render_mana_cost(cost: Iterable[dict[str, Any]] | None) -> str:
    if not cost:
        return ""
    parts: list[str] = []
    for entry in cost:
        if not isinstance(entry, dict):
            continue
        count = int(entry.get("count", 0) or 0)
        colors = [c for c in entry.get("color", []) if c != "ManaColor_NONE"]
        symbols = [MANA_SYMBOLS[c] for c in colors if c in MANA_SYMBOLS]
        if not symbols:
            if count:
                parts.append("{" + str(count) + "}")
            continue
        for symbol in symbols:
            parts.extend(["{" + symbol + "}"] * max(1, count))
    return "".join(parts)


class _Accumulator:
    """Rebuilds one game from a full snapshot plus every diff that follows it."""

    def __init__(self, observer: Callable[[ArenaGameState], None] | None = None) -> None:
        self.reset_game()
        self.seat_names: dict[int, str] = {}
        self.my_seat: int | None = None
        self.saw_gre = False
        # A post-match reviewer has to see every engine state, not just the one
        # that happens to be current when the UI next polls. Two seconds of
        # polling would skip whole combats.
        self.observer = observer

    def reset_game(self) -> None:
        # Everything here belongs to one game and must be dropped when the next
        # full snapshot arrives. The finished match's result in particular used
        # to survive into the next game and report it as already over.
        self.match_result = ""
        self.game_info: dict[str, Any] = {}
        self.turn_info: dict[str, Any] = {}
        self.players: dict[int, dict[str, Any]] = {}
        self.zones: dict[int, dict[str, Any]] = {}
        self.objects: dict[int, dict[str, Any]] = {}
        self.state_actions: list[dict[str, Any]] = []
        self.request_type = ""
        self.request_seats: list[int] = []
        self.request_prompt: dict[str, Any] = {}
        self.request_actions: list[dict[str, Any]] = []
        self.has_state = False

    # ---- ingestion -------------------------------------------------------

    def feed_line(self, line: str) -> None:
        start = line.find("{")
        if start < 0:
            return
        blob = line[start:]
        if '"greToClientEvent"' in blob:
            self._feed_json(blob, self._handle_gre_event)
        elif '"reservedPlayers"' in blob:
            self._feed_json(blob, self._handle_room_state)
        elif '"systemSeatId"' in blob and '"clientToMatchServiceMessageType"' in blob:
            self._feed_json(blob, self._handle_client_message)

    @staticmethod
    def _feed_json(blob: str, handler) -> None:
        try:
            payload = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        try:
            handler(payload)
        except (TypeError, AttributeError, KeyError, ValueError):
            # A malformed or unexpected record must never stop the watcher.
            return

    def _handle_client_message(self, payload: dict[str, Any]) -> None:
        seat = payload.get("payload", {}).get("systemSeatId")
        if isinstance(seat, int):
            self.my_seat = seat

    def _handle_room_state(self, payload: dict[str, Any]) -> None:
        event = payload.get("matchGameRoomStateChangedEvent", {})
        info = event.get("gameRoomInfo", {})
        for player in info.get("gameRoomConfig", {}).get("reservedPlayers", []) or []:
            seat = player.get("systemSeatId")
            name = player.get("playerName")
            if isinstance(seat, int) and name:
                self.seat_names[seat] = str(name)
        final = info.get("finalMatchResult")
        if final:
            for result in final.get("resultList", []) or []:
                if result.get("scope") == "MatchScope_Match":
                    self.match_result = self._describe_result(result.get("winningTeamId"))

    def _describe_result(self, winning_team: Any) -> str:
        if not isinstance(winning_team, int) or self.my_seat is None:
            return "The match is over."
        my_team = self.players.get(self.my_seat, {}).get("teamId", self.my_seat)
        return "You won the match." if winning_team == my_team else "You lost the match."

    def _game_outcome(self) -> tuple[str, str]:
        """Win or loss for the current game, from gameInfo's own result list."""
        for entry in self.game_info.get("results", []) or []:
            if entry.get("scope") != "MatchScope_Game":
                continue
            winning_team = entry.get("winningTeamId")
            reason = str(entry.get("reason", "")).replace("ResultReason_", "")
            if not isinstance(winning_team, int) or self.my_seat is None:
                return ("The game is over.", reason)
            my_team = self.players.get(self.my_seat, {}).get("teamId", self.my_seat)
            won = winning_team == my_team
            return ("You won this game." if won else "You lost this game.", reason)
        return ("", "")

    def _notify(self) -> None:
        if self.observer is None:
            return
        try:
            self.observer(self.build(None))
        except Exception:
            # Review is a bonus feature; it must never break log reading.
            pass

    def _handle_gre_event(self, payload: dict[str, Any]) -> None:
        messages = payload.get("greToClientEvent", {}).get("greToClientMessages", []) or []
        # Arena packs many engine states into one log line -- whole combats fit
        # inside a single record -- so an observer that fired once per line
        # would see about one state in five and miss most of the game.
        pending_request = False
        for message in messages:
            if not isinstance(message, dict):
                continue
            self.saw_gre = True
            seats = [s for s in (message.get("systemSeatIds") or []) if isinstance(s, int)]
            if len(seats) == 1:
                # Messages are addressed to this client, so a single seat id is mine.
                self.my_seat = seats[0]
            kind = message.get("type", "")
            if kind == "GREMessageType_GameStateMessage":
                self._apply_game_state(message.get("gameStateMessage", {}) or {})
                self._notify()
                pending_request = False
            elif kind.endswith("Req"):
                self.request_type = kind
                self.request_seats = seats
                self.request_prompt = message.get("prompt", {}) or {}
                body_key = kind.replace("GREMessageType_", "")
                body_key = body_key[0].lower() + body_key[1:]
                body = message.get(body_key, {}) or {}
                self.request_actions = body.get("actions", []) or []
                pending_request = True
        # A request that trails the last state message is a new moment of its
        # own: it is what the engine is asking you for right now.
        if pending_request:
            self._notify()

    def _apply_game_state(self, state: dict[str, Any]) -> None:
        if state.get("type") == "GameStateType_Full":
            self.reset_game()
        self.has_state = True
        if state.get("gameInfo"):
            self.game_info.update(state["gameInfo"])
        if state.get("turnInfo"):
            self.turn_info.update(state["turnInfo"])
        for player in state.get("players", []) or []:
            seat = player.get("systemSeatNumber", player.get("controllerSeatId"))
            if isinstance(seat, int):
                self.players.setdefault(seat, {}).update(player)
        for zone in state.get("zones", []) or []:
            zone_id = zone.get("zoneId")
            if isinstance(zone_id, int):
                self.zones.setdefault(zone_id, {}).update(zone)
        for obj in state.get("gameObjects", []) or []:
            instance = obj.get("instanceId")
            if isinstance(instance, int):
                # A diff resends the whole object, so replace rather than merge:
                # merging would keep a stale "attacking" flag after combat ends.
                self.objects[instance] = obj
        for deleted in state.get("diffDeletedInstanceIds", []) or []:
            self.objects.pop(deleted, None)
        if "actions" in state:
            self.state_actions = state.get("actions") or []

    # ---- projection ------------------------------------------------------

    def build(self, log_path: Path | None) -> ArenaGameState:
        database = load_card_database()
        seat = self.my_seat
        opponent_seat = None
        if seat is not None:
            opponent_seat = next((s for s in sorted(self.players) if s != seat), 2 if seat == 1 else 1)

        zone_index = {
            zone_id: (zone.get("type", ""), zone.get("ownerSeatId"))
            for zone_id, zone in self.zones.items()
        }

        def zone_ids(zone_type: str, owner: int | None) -> set[int]:
            return {
                zone_id
                for zone_id, (kind, zone_owner) in zone_index.items()
                if kind == zone_type and (owner is None or zone_owner == owner)
            }

        def cards_in(zone_type: str, owner: int | None = None) -> list[ArenaCard]:
            wanted = zone_ids(zone_type, owner)
            return [
                self._to_card(obj, database)
                for obj in self.objects.values()
                if obj.get("zoneId") in wanted
            ]

        def zone_count(zone_type: str, owner: int | None) -> int:
            if owner is None:
                return 0
            return sum(
                len(self.zones[zone_id].get("objectInstanceIds", []) or [])
                for zone_id in zone_ids(zone_type, owner)
            )

        battlefield = cards_in("ZoneType_Battlefield")
        mine = tuple(sorted((c for c in battlefield if c.controller == seat), key=_board_order))
        theirs = tuple(sorted((c for c in battlefield if c.controller != seat), key=_board_order))
        hand = tuple(cards_in("ZoneType_Hand", seat)) if seat is not None else ()
        stack = tuple(cards_in("ZoneType_Stack"))

        stage = self.game_info.get("stage", "")
        match_state = self.game_info.get("matchState", "")
        game_over = (
            stage == "GameStage_GameOver"
            or match_state in ("MatchState_MatchComplete", "MatchState_GameComplete")
            or self.request_type == "GREMessageType_IntermissionReq"
            or bool(self.match_result)
        )
        in_match = self.has_state and bool(self.players) and not game_over
        game_result, game_reason = self._game_outcome()

        request = ""
        if self.request_type and (not self.request_seats or seat in self.request_seats):
            request = REQUEST_LABELS.get(
                self.request_type,
                self.request_type.replace("GREMessageType_", "").replace("Req", ""),
            )
        prompt = database.prompt_text(self.request_prompt.get("promptId")) if database else ""

        # The engine only accepts a spell or ability while it is asking you for
        # one. Between requests its action list lingers, and offering a stale
        # sorcery-speed cast during, say, declare blockers would be wrong.
        decision_is_mine = seat is not None and self.turn_info.get("decisionPlayer") == seat
        offer_plays = decision_is_mine and self.request_type in (
            "",
            "GREMessageType_ActionsAvailableReq",
            "GREMessageType_PayCostsReq",
        )
        actions, mana_sources = self._build_actions(seat, database, offer_plays)
        if not mana_sources:
            mana_sources = sum(1 for card in mine if not card.tapped and "Land" in (card.type_line or ""))

        return ArenaGameState(
            available=True,
            detailed=self.saw_gre,
            in_match=in_match,
            reason="",
            my_seat=seat,
            my_name=self.seat_names.get(seat, "") if seat is not None else "",
            opponent_name=self.seat_names.get(opponent_seat, "") if opponent_seat is not None else "",
            my_life=self.players.get(seat, {}).get("lifeTotal") if seat is not None else None,
            opponent_life=self.players.get(opponent_seat, {}).get("lifeTotal") if opponent_seat is not None else None,
            turn_number=self.turn_info.get("turnNumber"),
            phase=self.turn_info.get("phase", ""),
            step=self.turn_info.get("step", ""),
            active_player=self.turn_info.get("activePlayer"),
            priority_player=self.turn_info.get("priorityPlayer"),
            decision_player=self.turn_info.get("decisionPlayer"),
            request=request,
            prompt=prompt,
            hand=hand,
            my_battlefield=mine,
            opponent_battlefield=theirs,
            stack=stack,
            my_graveyard=zone_count("ZoneType_Graveyard", seat),
            opponent_graveyard=zone_count("ZoneType_Graveyard", opponent_seat),
            my_library=zone_count("ZoneType_Library", seat),
            opponent_library=zone_count("ZoneType_Library", opponent_seat),
            opponent_hand=zone_count("ZoneType_Hand", opponent_seat),
            actions=actions,
            mana_sources_available=mana_sources,
            game_over=game_over,
            result=self.match_result if game_over else "",
            log_path=log_path,
            match_id=str(self.game_info.get("matchID", "") or ""),
            game_number=self.game_info.get("gameNumber"),
            game_result=game_result,
            game_result_reason=game_reason,
        )

    def _to_card(self, obj: dict[str, Any], database) -> ArenaCard:
        grp_id = int(obj.get("grpId", 0) or 0)
        name = ""
        type_line = ""
        if database is not None:
            name = database.localized(obj.get("name")) or database.card_name(grp_id)
            type_line = database.type_line(grp_id)
        # cardTypes comes from the engine, so it stays right for face-down cards,
        # copies, and anything whose printed type line has been changed.
        card_types = tuple(
            str(entry).replace("CardType_", "").casefold()
            for entry in (obj.get("cardTypes") or [])
        )
        blocked = tuple(
            int(attacker)
            for attacker in ((obj.get("blockInfo") or {}).get("attackerIds") or [])
            if isinstance(attacker, int)
        )
        return ArenaCard(
            instance_id=int(obj.get("instanceId", 0) or 0),
            grp_id=grp_id,
            name=name,
            type_line=type_line,
            power=_value_of(obj.get("power")),
            toughness=_value_of(obj.get("toughness")),
            damage=int(obj.get("damage", 0) or 0),
            tapped=bool(obj.get("isTapped")),
            summoning_sick=bool(obj.get("hasSummoningSickness")),
            attacking=obj.get("attackState") not in (None, "AttackState_None"),
            blocking=obj.get("blockState") not in (None, "BlockState_None"),
            controller=obj.get("controllerSeatId", obj.get("ownerSeatId")),
            card_types=card_types,
            abilities=_ability_texts(obj, database),
            blocked_attackers=blocked,
        )

    def _build_actions(
        self, seat: int | None, database, offer_plays: bool = True
    ) -> tuple[tuple[ArenaAction, ...], int]:
        # The request's own action list is the authoritative "right now" set;
        # the game-state list is the fallback between requests.
        raw: list[dict[str, Any]] = []
        if self.request_actions:
            raw = [a for a in self.request_actions if isinstance(a, dict)]
        else:
            for entry in self.state_actions:
                if not isinstance(entry, dict):
                    continue
                if seat is not None and entry.get("seatId") not in (None, seat):
                    continue
                action = entry.get("action")
                if isinstance(action, dict):
                    raw.append(action)

        built: list[ArenaAction] = []
        mana_sources = 0
        seen: set[tuple[str, str, str]] = set()
        for action in raw:
            action_type = action.get("actionType", "")
            if action_type in MANA_ACTIONS:
                # Between requests the engine's action list can be a step stale,
                # so only count a source the board still shows as untapped.
                source = self.objects.get(action.get("instanceId"), {})
                if not source.get("isTapped"):
                    mana_sources += 1
                continue
            if not offer_plays or action_type not in MEANINGFUL_ACTIONS:
                continue
            grp_id = action.get("grpId") or self.objects.get(action.get("instanceId"), {}).get("grpId")
            card = (database.card_name(grp_id) if database is not None else "") or f"card #{action.get('instanceId', '?')}"
            detail = ""
            if database is not None and action.get("abilityGrpId"):
                detail = _shorten(database.ability_text(action["abilityGrpId"]).replace("CARDNAME", card))
            key = (action_type, card, detail)
            if key in seen:
                continue
            seen.add(key)
            built.append(ArenaAction(action_type, card, render_mana_cost(action.get("manaCost")), detail))
        return tuple(built[:12]), mana_sources


def _ability_texts(obj: dict[str, Any], database) -> tuple[str, ...]:
    """Resolve the object's live abilities to lower-case rules text.

    `uniqueAbilities` entries look like {"id": ordinal, "grpId": ability}. The
    grpId is the ability itself; the id is only its slot on the card. Reading
    the live object rather than the printed card keeps granted keywords -- an
    aura's flying, an anthem's counters -- in the answer.
    """
    if database is None:
        return ("unresolved rules text",)
    texts: list[str] = []
    for entry in obj.get("uniqueAbilities") or []:
        ability_id = entry.get("grpId") if isinstance(entry, dict) else entry
        text = database.ability_text(ability_id)
        if text:
            texts.append(" ".join(text.split()).casefold())
        else:
            texts.append("unresolved rules text")
    return tuple(texts)


def _value_of(field_value: Any) -> int | None:
    if isinstance(field_value, dict):
        value = field_value.get("value")
        return int(value) if isinstance(value, (int, float)) else 0
    if isinstance(field_value, (int, float)):
        return int(field_value)
    return None


def _board_order(card: ArenaCard) -> tuple[int, str]:
    is_land = "Land" in (card.type_line or "")
    return (1 if is_land else 0, card.name or "")


def _shorten(text: str, limit: int = 110) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


class ArenaLogReader:
    """Incrementally tails Player.log so a two-second poll stays cheap."""

    def __init__(self, observer: Callable[[ArenaGameState], None] | None = None) -> None:
        self._path: Path | None = None
        self._offset = 0
        self._file_identity = None
        self._partial = ""
        self._observer = observer
        self._accumulator = _Accumulator(observer)
        # The watcher thread and the chat worker both poll this reader, and the
        # tailing offset must not be advanced by two threads at once.
        self._lock = threading.Lock()

    def _reset(self, path: Path, size: int) -> None:
        self._path = path
        self._partial = ""
        self._accumulator = _Accumulator(self._observer)
        self._offset = max(0, size - INITIAL_TAIL_BYTES)
        self._discard_initial_fragment = self._offset > 0

    def poll(self) -> ArenaGameState:
        with self._lock:
            return self._poll_locked()

    def _poll_locked(self) -> ArenaGameState:
        path = find_player_log()
        if path is None:
            return ArenaGameState(
                available=False,
                reason=(
                    "Arena's Player.log was not found. Start MTG Arena once so it creates "
                    f"{default_log_directory()}."
                ),
            )
        try:
            stat = path.stat()
            size = stat.st_size
            identity = (stat.st_dev, stat.st_ino)
        except OSError as exc:
            return ArenaGameState(available=False, reason=f"Arena's Player.log could not be read: {exc}")

        if self._path != path or size < self._offset or identity != self._file_identity:
            self._reset(path, size)
            self._file_identity = identity
        # A cold start seeks into the middle of the file, so the first line read
        # is usually a fragment and must be discarded.
        started_mid_file = getattr(self, "_discard_initial_fragment", False)

        if size > self._offset:
            try:
                with path.open("rb") as handle:
                    handle.seek(self._offset)
                    chunk = handle.read(size - self._offset)
            except OSError as exc:
                return ArenaGameState(available=True, reason=f"Arena's Player.log could not be read: {exc}")
            self._offset = size
            text = self._partial + chunk.decode("utf-8", errors="replace")
            lines = text.split("\n")
            # The final fragment may be a half-written line; keep it for next poll.
            self._partial = lines.pop() if lines else ""
            if started_mid_file and lines:
                lines.pop(0)
                self._discard_initial_fragment = False
            for line in lines:
                self._accumulator.feed_line(line)

        state = self._accumulator.build(path)
        if not state.detailed:
            return ArenaGameState(
                available=True,
                detailed=False,
                log_path=path,
                reason=(
                    "Arena is writing Player.log, but it contains no game records. "
                    "Turn on Detailed Logs: Arena, gear icon, View Account, tick "
                    "Detailed Logs (Plugin Support), then restart Arena."
                ),
            )
        return state


_observer: Callable[[ArenaGameState], None] | None = None
_reader = ArenaLogReader()


def set_state_observer(observer: Callable[[ArenaGameState], None] | None) -> None:
    """Receive every engine state the reader parses, not just the polled one.

    The post-match reviewer needs the whole game, and a two-second poll only
    ever shows the state that happens to be current when it fires.
    """
    global _observer, _reader
    _observer = observer
    _reader = ArenaLogReader(observer)


def read_arena_state() -> ArenaGameState:
    """Poll the shared incremental reader for the current Arena game state."""
    return _reader.poll()


def reset_reader() -> None:
    """Drop the tailing position, e.g. after the user restarts Arena."""
    global _reader
    _reader = ArenaLogReader(_observer)


def render_state(state: ArenaGameState) -> str:
    """A full, plain-text board report for the chat pane."""
    if not state.available or not state.detailed:
        return state.reason
    if not state.in_match:
        if state.game_over and state.result:
            return f"No game is in progress. {state.result}"
        return "Arena detailed logs are readable, but no game is in progress right now."

    lines: list[str] = []
    whose = "Your turn" if state.my_turn else "Opponent's turn"
    header = f"Turn {state.turn_number or '?'} - {whose}"
    readable_phase = describe_phase(state.phase, state.step)
    if readable_phase:
        header += f" - {readable_phase}"
    lines.append(header)

    me = state.my_name or "You"
    them = state.opponent_name or "Opponent"
    lines.append(f"Life: {me} {state.my_life} - {them} {state.opponent_life}")

    if state.request:
        ask = state.request
        if state.prompt:
            ask += f" ({state.prompt})"
        lines.append(f"Arena is asking you to: {ask}")
    elif state.waiting_on_me:
        lines.append("Arena is waiting on you.")
    else:
        lines.append("Arena is waiting on your opponent.")

    if state.stack:
        lines.append("")
        lines.append("On the stack:")
        lines.extend(f"  - {card.describe()}" for card in state.stack)
    if state.hand:
        lines.append("")
        lines.append(f"Your hand ({len(state.hand)}):")
        lines.extend(f"  - {card.describe()}" for card in state.hand)
    if state.my_battlefield:
        lines.append("")
        lines.append(f"Your battlefield ({len(state.my_battlefield)}):")
        lines.extend(f"  - {card.describe()}" for card in state.my_battlefield)
    if state.opponent_battlefield:
        lines.append("")
        lines.append(f"{them}'s battlefield ({len(state.opponent_battlefield)}):")
        lines.extend(f"  - {card.describe()}" for card in state.opponent_battlefield)

    lines.append("")
    lines.append(
        f"Zones: your library {state.my_library}, graveyard {state.my_graveyard}; "
        f"{them} hand {state.opponent_hand}, library {state.opponent_library}, "
        f"graveyard {state.opponent_graveyard}."
    )
    if state.mana_sources_available:
        lines.append(f"Untapped mana sources you control: {state.mana_sources_available}.")
    return "\n".join(lines)
