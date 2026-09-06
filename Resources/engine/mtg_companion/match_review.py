"""Turn a finished Arena game into plain-language coaching.

Every judgement here is derived from Arena's own rules-engine records: what the
engine offered you, what you actually submitted, and what the board looked like
one state later. Nothing is guessed from pixels, and nothing is invented.

The reviewer is deliberately conservative. A combat is only graded when every
creature involved has rules text the reviewer can fully account for -- the
evergreen keywords in `arena_state.COMBAT_KEYWORDS`. A creature with a triggered
ability, an activated pump, or any other sentence of text makes the reviewer
stay quiet about that combat rather than call a good play a blunder.
"""

from __future__ import annotations

import threading
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .arena_state import COMBAT_KEYWORDS, ArenaCard, ArenaGameState, set_state_observer

ROOT = Path(__file__).resolve().parent.parent
REVIEW_DIR = ROOT / "data" / "match-reviews"

BLUNDER = "blunder"
MISTAKE = "mistake"
GOOD = "good"
NOTE = "note"

# Blunders first: the point of the review is to fix the expensive habits before
# celebrating the cheap wins.
GRADE_ORDER = {BLUNDER: 0, MISTAKE: 1, GOOD: 2, NOTE: 3}
GRADE_HEADINGS = {
    BLUNDER: "Blunders — fix these first",
    MISTAKE: "Mistakes — worth a second look",
    GOOD: "Good moves — keep doing these",
    NOTE: "Worth knowing",
}
# How many of each grade survive into the final report. Blunders are never
# trimmed; a wall of forty "good block" lines would bury them.
GRADE_LIMITS = {BLUNDER: 8, MISTAKE: 8, GOOD: 6, NOTE: 5}
# Reviews are written to disk as they are filed, so the in-memory list only
# has to cover the current session's match.
MAX_KEPT_REVIEWS = 30


@dataclass(frozen=True)
class ReviewNote:
    grade: str
    turn: int
    title: str
    detail: str

    def render(self) -> str:
        where = f"Turn {self.turn}: " if self.turn else ""
        return f"{where}{self.title}\n    {self.detail}"


@dataclass
class GameReview:
    match_id: str
    game_number: int
    opponent: str
    result: str
    reason: str
    turns: int
    my_life: int | None
    opponent_life: int | None
    notes: list[ReviewNote]
    finished_at: str

    @property
    def headline(self) -> str:
        game = f"Game {self.game_number}" if self.game_number else "Game"
        against = f" vs {self.opponent}" if self.opponent else ""
        return f"{game}{against} — {self.result or 'result unknown'}"

    def counts(self) -> dict[str, int]:
        return {
            grade: sum(1 for note in self.notes if note.grade == grade)
            for grade in (BLUNDER, MISTAKE, GOOD, NOTE)
        }

    def grouped(self) -> list[tuple[str, list[ReviewNote]]]:
        groups = []
        for grade in (BLUNDER, MISTAKE, GOOD, NOTE):
            chosen = [note for note in self.notes if note.grade == grade]
            if chosen:
                groups.append((grade, chosen))
        return groups

    def render(self) -> str:
        tally = self.counts()
        lines = [
            self.headline,
            f"Lasted {self.turns} turn(s). Final life: you {self.my_life}, "
            f"{self.opponent or 'opponent'} {self.opponent_life}.",
            f"{tally[BLUNDER]} blunder(s), {tally[MISTAKE]} mistake(s), "
            f"{tally[GOOD]} good move(s).",
        ]
        if self.reason and self.reason not in ("Game", ""):
            lines.append(f"Ended by: {self.reason}.")
        for grade, notes in self.grouped():
            lines.extend(["", GRADE_HEADINGS[grade]])
            lines.extend(f"  - {note.render()}" for note in notes)
        if not self.notes:
            lines.extend(["", "Nothing stood out as clearly right or clearly wrong in this game."])
        lines.extend(
            [
                "",
                "Read from Arena's own engine records. Combats with rules text I cannot "
                "fully account for are left ungraded rather than guessed at.",
            ]
        )
        return "\n".join(lines)

    def to_markdown(self) -> str:
        tally = self.counts()
        lines = [
            f"# {self.headline}",
            "",
            f"- Finished: {self.finished_at}",
            f"- Turns: {self.turns}",
            f"- Final life: you {self.my_life}, opponent {self.opponent_life}",
            f"- Tally: {tally[BLUNDER]} blunder(s), {tally[MISTAKE]} mistake(s), "
            f"{tally[GOOD]} good move(s), {tally[NOTE]} note(s)",
            "",
        ]
        for grade, notes in self.grouped():
            lines.extend([f"## {GRADE_HEADINGS[grade]}", ""])
            for note in notes:
                where = f"**Turn {note.turn}** — " if note.turn else ""
                lines.append(f"- {where}{note.title}  ")
                lines.append(f"  {note.detail}")
            lines.append("")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Combat arithmetic
# --------------------------------------------------------------------------


def _kills(dealer: ArenaCard, target: ArenaCard) -> bool:
    """Would `dealer`'s combat damage destroy `target` this combat?"""
    if "indestructible" in target.keywords:
        return False
    power = dealer.power or 0
    if power <= 0:
        return False
    if "deathtouch" in dealer.keywords:
        return True
    if "double strike" in dealer.keywords:
        power *= 2
    return power >= target.toughness_left


def _can_block(blocker: ArenaCard, attacker: ArenaCard) -> bool:
    """Is this single blocker legally able to block this attacker?"""
    attacker_keywords = attacker.keywords
    if "menace" in attacker_keywords:
        # Legal only with a second blocker, so never suggest it as a solo block.
        return False
    if "flying" in attacker_keywords and not (blocker.keywords & {"flying", "reach"}):
        return False
    if "defender" in attacker.keywords:  # not an attacker at all
        return False
    return True


# Rules text that can change whether combat damage kills, or whether an attack
# can be blocked at all. A creature carrying any of it is one the reviewer will
# not second-guess: it says what happened and stops there. Plain triggers such
# as "when this enters, draw a card" are not in here, because they change
# nothing about the exchange itself.
RISKY_TEXT = (
    "block",
    "damage",
    "destroy",
    "exile",
    "sacrifice",
    "regenerat",
    "prevent",
    "protection",
    "indestructible",
    "fight",
    "get +",
    "gets +",
    "gains ",
    "gain ",
    "counter on",
    "-1/-1",
    "+1/+1",
    "can't",
    "cannot",
)


def _rules_text(card: ArenaCard) -> list[str]:
    """The card's ability text minus the keywords already modelled exactly."""
    return [text for text in card.abilities if text.strip().rstrip(".") not in COMBAT_KEYWORDS]


def _combat_clear(card: ArenaCard) -> bool:
    """True when nothing in this card's text can overturn the combat maths."""
    return "unresolved rules text" not in card.abilities and not any(term in text for text in _rules_text(card) for term in RISKY_TEXT)


def _can_be_blocked_normally(card: ArenaCard) -> bool:
    """A blocker whose own text restricts blocking must not be volunteered."""
    return not any("block" in text for text in _rules_text(card))


def _name(card: ArenaCard | None) -> str:
    if card is None:
        return "a creature"
    return card.name or f"card #{card.instance_id}"


def _stats(card: ArenaCard) -> str:
    if card.power is None or card.toughness is None:
        return _name(card)
    return f"{_name(card)} ({card.power}/{card.toughness})"


def _listing(cards: list[ArenaCard]) -> str:
    return ", ".join(_stats(card) for card in cards) or "nothing"


@dataclass
class _Combat:
    turn: int
    mine: bool
    my_life: int
    opponent_life: int
    attackers: dict[int, ArenaCard] = field(default_factory=dict)
    blocks: dict[int, list[ArenaCard]] = field(default_factory=dict)
    blockers_available: dict[int, ArenaCard] = field(default_factory=dict)
    my_dead: list[ArenaCard] = field(default_factory=list)
    opponent_dead: list[ArenaCard] = field(default_factory=list)


# --------------------------------------------------------------------------
# One game
# --------------------------------------------------------------------------


class _GameTracker:
    """Accumulates one game's decisions as the engine reports them."""

    def __init__(self, state: ArenaGameState) -> None:
        self.match_id = state.match_id
        self.game_number = state.game_number or 1
        self.opponent = state.opponent_name
        self.notes: list[ReviewNote] = []
        self.known: dict[int, ArenaCard] = {}
        self.my_ids: set[int] | None = None
        self.opponent_ids: set[int] | None = None
        self.stack_seen: set[int] = set()
        self.mulligan_hands: list[tuple[str, ...]] = []
        self.combat: _Combat | None = None
        self.pending_attack: tuple[list[ArenaCard], list[ArenaCard], int] | None = None
        self.turn = 0
        self.turn_is_mine = False
        self.last: ArenaGameState = state
        self.max_turn = 0
        self.land_drops_missed = 0
        self.land_drop_turns = 0
        self.full_mana_turns = 0
        self._start_turn(state)

    # ---- turn bookkeeping ------------------------------------------------

    def _start_turn(self, state: ArenaGameState) -> None:
        self.turn = state.turn_number or 0
        self.max_turn = max(self.max_turn, self.turn)
        self.turn_is_mine = state.my_turn
        self.turn_land_offered = False
        self.turn_lands_played = 0
        self.turn_casts: list[str] = []
        self.turn_saw_main = False
        self.turn_end_mana = 0
        self.turn_hand_size = len(state.hand)
        self.turn_final_offer: tuple[int, list[str]] = (0, [])

    def _close_turn(self) -> None:
        if not (self.turn_is_mine and self.turn_saw_main and self.turn):
            return
        if self.turn_land_offered:
            self.land_drop_turns += 1
        if self.turn_land_offered and self.turn_lands_played == 0:
            self.land_drops_missed += 1
            # Early land drops decide games; a late one is often a real choice.
            grade = BLUNDER if self.turn <= 6 else MISTAKE
            self._add(
                grade,
                "Missed your land drop",
                "Arena was offering you a land to play from hand and the turn ended "
                "without one. A land drop is free, it is once per turn, and it never "
                "comes back. Play the land before you decide anything else.",
            )
        if self.turn_casts and self.turn_end_mana == 0:
            self.full_mana_turns += 1
        mana, castable = self.turn_final_offer
        if castable and mana:
            names = ", ".join(dict.fromkeys(castable))
            self._add(
                MISTAKE,
                f"Ended the turn with {mana} untapped source(s) and {names} unplayed",
                "Arena was still offering that cast and its cost fits inside the mana you "
                "had left, so unless your colours did not line up you could have made "
                "that play. Nothing there was an instant or had flash, so there was "
                "nothing to hold the mana up for, and mana does not carry over.",
            )
        elif self.turn_end_mana >= 2 and not self.turn_casts and self.turn_hand_size:
            self._add(
                NOTE,
                f"Ended the turn with {self.turn_end_mana} untapped sources and nothing cast",
                f"You had {self.turn_hand_size} card(s) in hand and nothing in them was "
                "castable. That is a mana-curve problem to look at in deckbuilding, not "
                "a play mistake.",
            )

    # ---- ingestion -------------------------------------------------------

    def observe(self, state: ArenaGameState) -> None:
        if state.turn_number and state.turn_number != self.turn:
            self._close_combat(state)
            self._close_turn()
            self._start_turn(state)
        if state.opponent_name:
            self.opponent = state.opponent_name
        self._track_mulligan(state)
        self._track_board(state)
        self._track_turn(state)
        self._track_combat(state)
        self.last = state

    def _track_mulligan(self, state: ArenaGameState) -> None:
        if state.request != "Keep or mulligan" or not state.hand:
            return
        hand = tuple(sorted(card.name or str(card.instance_id) for card in state.hand))
        if not self.mulligan_hands or self.mulligan_hands[-1] != hand:
            self.mulligan_hands.append(hand)

    def _track_board(self, state: ArenaGameState) -> None:
        for card in state.my_battlefield + state.opponent_battlefield + state.hand:
            self.known[card.instance_id] = card
        my_ids = {card.instance_id for card in state.my_battlefield}
        opponent_ids = {card.instance_id for card in state.opponent_battlefield}
        if self.my_ids is not None:
            for gone in self.my_ids - my_ids:
                self._record_loss(mine=True, card=self.known.get(gone))
            for arrived in my_ids - self.my_ids:
                card = self.known.get(arrived)
                if card is not None and card.is_land and self.turn_is_mine:
                    self.turn_lands_played += 1
        if self.opponent_ids is not None:
            for gone in self.opponent_ids - opponent_ids:
                self._record_loss(mine=False, card=self.known.get(gone))
        self.my_ids, self.opponent_ids = my_ids, opponent_ids

    def _record_loss(self, mine: bool, card: ArenaCard | None) -> None:
        if card is None or not card.is_creature or self.combat is None:
            return
        (self.combat.my_dead if mine else self.combat.opponent_dead).append(card)

    def _track_turn(self, state: ArenaGameState) -> None:
        for card in state.stack:
            if card.controller == state.my_seat and card.instance_id not in self.stack_seen:
                self.stack_seen.add(card.instance_id)
                self.turn_casts.append(_name(card))
        if not state.my_turn:
            return
        if any(
            action.action_type in ("ActionType_Play", "ActionType_Play_MDFC_Back")
            for action in state.actions
        ):
            self.turn_land_offered = True
        if state.phase in ("Phase_Main1", "Phase_Main2") and state.waiting_on_me:
            self.turn_saw_main = True
        if state.phase in ("Phase_Main2", "Phase_Ending") and state.waiting_on_me:
            # Arena only ever lists actions its engine will actually accept, so
            # a cast still on offer here was affordable and legal right then.
            # Holding mana up for an instant is correct play, not a mistake,
            # so anything castable on the opponent's turn is excluded here.
            held_deliberately = {
                card.name
                for card in state.hand
                if "instant" in card.card_types or "flash" in card.keywords
            }
            castable = [
                f"{action.card} ({action.mana_cost})"
                for action in state.actions
                if action.action_type.startswith("ActionType_Cast")
                and action.card not in held_deliberately
                and 0 < action.cost_value <= state.mana_sources_available
            ]
            self.turn_final_offer = (state.mana_sources_available, castable)
        self.turn_end_mana = state.mana_sources_available
        self.turn_hand_size = len(state.hand)

    # ---- combat ----------------------------------------------------------

    def _track_combat(self, state: ArenaGameState) -> None:
        if state.phase != "Phase_Combat":
            # Closing on "no creature is attacking" would split one combat in
            # two, because the engine briefly reports an empty attack between
            # the first-strike and regular damage steps.
            self._close_combat(state)
            return
        attackers = [
            card
            for card in state.my_battlefield + state.opponent_battlefield
            if card.attacking
        ]
        if state.step == "Step_DeclareAttack" and state.my_turn and not attackers:
            # Snapshot the choice Arena is offering before any attack is declared.
            self.pending_attack = (
                [card for card in state.my_battlefield if _can_attack(card)],
                [card for card in state.opponent_battlefield if card.is_creature],
                state.opponent_life or 0,
            )
        if not attackers:
            return
        if self.combat is None:
            self.combat = _Combat(
                turn=self.turn,
                mine=state.my_turn,
                my_life=state.my_life or 0,
                opponent_life=state.opponent_life or 0,
                attackers={card.instance_id: card for card in attackers},
            )
            if state.my_turn:
                self._judge_attack_declaration(attackers)
        self.combat.attackers.update({card.instance_id: card for card in attackers})
        if not self.combat.mine:
            self._track_blockers(state)

    def _track_blockers(self, state: ArenaGameState) -> None:
        combat = self.combat
        if combat is None:
            return
        declared = [card for card in state.my_battlefield if card.blocked_attackers]
        if not declared and state.step == "Step_DeclareBlock":
            # Everything untapped is still a candidate right up until blockers
            # are locked in, so keep re-taking the snapshot until they are.
            combat.blockers_available = {
                card.instance_id: card
                for card in state.my_battlefield
                if card.is_creature and not card.tapped and not card.attacking
            }
        for card in declared:
            combat.blockers_available.setdefault(card.instance_id, card)
            for attacker_id in card.blocked_attackers:
                blocking = combat.blocks.setdefault(attacker_id, [])
                if all(other.instance_id != card.instance_id for other in blocking):
                    blocking.append(card)

    def _judge_attack_declaration(self, attackers: list[ArenaCard]) -> None:
        if self.pending_attack is None:
            return
        able, their_creatures, their_life = self.pending_attack
        self.pending_attack = None
        declared = {card.instance_id for card in attackers}
        held_back = [card for card in able if card.instance_id not in declared]
        if not held_back:
            return
        untapped_defenders = [card for card in their_creatures if not card.tapped]
        if untapped_defenders:
            return  # They could block; holding a creature back is a judgement call.
        total = sum(card.power or 0 for card in able)
        if their_life and total >= their_life and all(_combat_clear(c) for c in their_creatures):
            self._add(
                BLUNDER,
                "You had lethal on board and did not take it",
                f"Your opponent was at {their_life} with no untapped blockers. "
                f"Attacking with everything ({_listing(able)}) was {total} damage and "
                "wins the game on the spot. Count their life against your total power "
                "before you declare.",
            )
            return
        held_power = sum(card.power or 0 for card in held_back)
        if not held_power:
            return
        if not their_creatures:
            self._add(
                MISTAKE,
                f"Left {held_power} power at home against an empty board",
                f"Your opponent controlled no creatures at all, so {_listing(held_back)} "
                "could not have been blocked and had nothing to stay home and block. "
                "That was free damage.",
            )
        else:
            self._add(
                NOTE,
                f"Held back {_listing(held_back)} with no untapped blockers opposing",
                "Nothing could block this turn. Holding a creature to block next turn "
                "is fine — just make sure that was the plan and not hesitation.",
            )

    def _close_combat(self, state: ArenaGameState) -> None:
        combat = self.combat
        self.combat = None
        self.pending_attack = None
        if combat is None or not combat.attackers:
            return
        if combat.mine:
            self._judge_my_attack(combat, state)
        else:
            self._judge_my_blocks(combat, state)

    def _judge_my_attack(self, combat: _Combat, state: ArenaGameState) -> None:
        dealt = combat.opponent_life - (state.opponent_life or 0)
        lost, killed = combat.my_dead, combat.opponent_dead
        if dealt > 0 and not lost:
            self._add(
                GOOD,
                f"Attack connected for {dealt} and cost you nothing",
                f"{_listing(list(combat.attackers.values()))} got through. "
                "Damage you take for free is the cheapest damage in the game.",
                turn=combat.turn,
            )
        if killed and len(killed) > len(lost):
            self._add(
                GOOD,
                f"Traded up in combat: killed {_listing(killed)}",
                f"You lost {_listing(lost)}. Winning the exchange on cards is how "
                "attacking creature decks grind out board control.",
                turn=combat.turn,
            )
        elif lost and not killed and dealt <= 0:
            self._add(
                MISTAKE,
                f"Attack cost you {_listing(lost)} and achieved nothing",
                "No damage got through and nothing of theirs died. Either every block "
                "they could make was good for them, or they had a trick. Before "
                "attacking, line each of their untapped creatures up against yours and "
                "ask what the best block available to them does.",
                turn=combat.turn,
            )

    def _judge_my_blocks(self, combat: _Combat, state: ArenaGameState) -> None:
        taken = combat.my_life - (state.my_life or 0)
        blocked_ids = set(combat.blocks)
        unblocked = [card for card in combat.attackers.values() if card.instance_id not in blocked_ids]
        used = {c.instance_id for group in combat.blocks.values() for c in group}
        spare = [card for card in combat.blockers_available.values() if card.instance_id not in used]

        for attacker in unblocked:
            if not _combat_clear(attacker):
                # Their creature has text that could punish or dodge a block.
                # Second-guessing it would be a guess, so stay quiet.
                continue
            free = [
                blocker
                for blocker in spare
                if _can_block(blocker, attacker)
                and _can_be_blocked_normally(blocker)
                and _kills(blocker, attacker)
                and not _kills(attacker, blocker)
            ]
            if not free:
                continue
            fatal = (state.my_life or 0) <= 0
            self._add(
                BLUNDER if fatal or (state.my_life or 99) <= 5 else MISTAKE,
                f"Free block available on {_stats(attacker)} and you took the damage",
                f"{_stats(free[0])} blocks it, kills it, and survives. "
                f"Instead you took {max(taken, attacker.power or 0)} damage and their "
                "creature is still there. A block that kills and survives is never wrong "
                "unless you are playing around a specific trick.",
                turn=combat.turn,
            )
            break

        for attacker_id, group in combat.blocks.items():
            attacker = combat.attackers.get(attacker_id)
            if attacker is None or len(group) != 1:
                continue
            blocker = group[0]
            died = any(card.instance_id == blocker.instance_id for card in combat.my_dead)
            attacker_died = any(card.instance_id == attacker_id for card in combat.opponent_dead)
            if attacker_died and not died:
                self._add(
                    GOOD,
                    f"Clean block: {_stats(blocker)} killed {_stats(attacker)} and lived",
                    "You removed a threat, took no damage, and kept the creature. "
                    "This is the best outcome a block has.",
                    turn=combat.turn,
                )
                continue
            if not died or attacker_died:
                continue
            better = [
                other
                for other in spare
                if _can_block(other, attacker)
                and _can_be_blocked_normally(other)
                and _kills(other, attacker)
                and not _kills(attacker, other)
            ]
            if better and _combat_clear(attacker):
                self._add(
                    MISTAKE,
                    f"Blocked {_stats(attacker)} with the wrong creature",
                    f"{_stats(blocker)} died and the attacker lived. "
                    f"{_stats(better[0])} was untapped and unused: it kills "
                    f"{_name(attacker)} and survives. When you have a blocker that wins "
                    "the fight outright, the chump block costs you a creature for nothing.",
                    turn=combat.turn,
                )
            elif combat.my_life > 12:
                self._add(
                    NOTE,
                    f"Chump-blocked {_stats(attacker)} at {combat.my_life} life",
                    f"You gave up {_stats(blocker)} to stop {attacker.power or 0} damage "
                    "while comfortably ahead on life. Life is a resource — spend it when "
                    "the creature is worth more than the points.",
                    turn=combat.turn,
                )

    # ---- output ----------------------------------------------------------

    def _add(self, grade: str, title: str, detail: str, turn: int | None = None) -> None:
        self.notes.append(
            ReviewNote(grade=grade, turn=self.turn if turn is None else turn, title=title, detail=detail)
        )

    def finish(self, state: ArenaGameState) -> GameReview:
        self._close_combat(state)
        self._close_turn()
        self._add_opening_hand_note()
        self._add_endgame_notes(state)
        return GameReview(
            match_id=self.match_id,
            game_number=self.game_number,
            opponent=self.opponent,
            result=state.game_result or state.result,
            reason=state.game_result_reason,
            turns=self.max_turn,
            my_life=state.my_life,
            opponent_life=state.opponent_life,
            notes=_rank(self.notes),
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    def _add_opening_hand_note(self) -> None:
        if not self.mulligan_hands:
            return
        kept = self.mulligan_hands[-1]
        mulligans = len(self.mulligan_hands) - 1
        lands = sum(
            1
            for name in kept
            if any(card.is_land for card in self.known.values() if (card.name or "") == name)
        )
        if len(kept) < 7 or not lands:
            return
        if lands <= 1:
            self._add(
                NOTE,
                f"Kept a {len(kept)}-card hand with {lands} land",
                "One-landers win when they draw perfectly and lose the other times. "
                f"You mulliganed {mulligans} time(s) this game.",
                turn=0,
            )
        elif lands >= 6:
            self._add(
                NOTE,
                f"Kept a {len(kept)}-card hand with {lands} lands",
                "A hand this land-heavy will flood out. Note whether the game was "
                "decided by drawing lands you did not need.",
                turn=0,
            )

    def _add_endgame_notes(self, state: ArenaGameState) -> None:
        won = state.game_result.startswith("You won")
        if won:
            self._add(GOOD, "You won this game", "Whatever the plan was, it closed.", turn=0)
        if self.land_drop_turns and not self.land_drops_missed and self.max_turn >= 4:
            self._add(
                GOOD,
                "Hit every land drop Arena offered you",
                "Mana consistency is the least glamorous and most reliable edge in Magic.",
                turn=0,
            )
        if self.full_mana_turns >= 2:
            self._add(
                GOOD,
                f"Emptied your mana on {self.full_mana_turns} turn(s)",
                "Unused mana is unused cards. Turns where every source got spent are "
                "the turns that build a board.",
                turn=0,
            )
        if not won and state.hand:
            names = ", ".join(_name(card) for card in state.hand[:5])
            self._add(
                NOTE,
                f"Lost holding {len(state.hand)} card(s)",
                f"Still in hand: {names}. Cards in hand do nothing. If they were "
                "uncastable, the deck wanted more lands; if they were castable, you "
                "waited too long.",
                turn=0,
            )


def _can_attack(card: ArenaCard) -> bool:
    return (
        card.is_creature
        and not card.tapped
        and not card.summoning_sick
        and "defender" not in card.keywords
        and (card.power or 0) > 0
    )


def _rank(notes: list[ReviewNote]) -> list[ReviewNote]:
    """Keep the worst mistakes and a representative sample of everything else."""
    kept: list[ReviewNote] = []
    for grade in (BLUNDER, MISTAKE, GOOD, NOTE):
        chosen = [note for note in notes if note.grade == grade]
        seen: set[str] = set()
        deduped = []
        for note in chosen:
            if note.title in seen and grade in (GOOD, NOTE):
                continue
            seen.add(note.title)
            deduped.append(note)
        kept.extend(deduped[: GRADE_LIMITS[grade]])
    return sorted(kept, key=lambda note: (GRADE_ORDER[note.grade], note.turn))


# --------------------------------------------------------------------------
# The whole match
# --------------------------------------------------------------------------


class MatchReviewer:
    """Watches every engine state and files a review when each game ends."""

    def __init__(self) -> None:
        # The log reader can be polled from the watcher thread and the chat
        # worker at once, so both the tracker and the finished list are guarded.
        self._lock = threading.Lock()
        self._tracker: _GameTracker | None = None
        self._pending: list[GameReview] = []
        self.reviews: list[GameReview] = []

    def observe(self, state: ArenaGameState) -> None:
        if not state.available or not state.detailed:
            return
        with self._lock:
            self._observe_locked(state)

    def _observe_locked(self, state: ArenaGameState) -> None:
        tracker = self._tracker
        if tracker is not None and not _same_game(tracker, state):
            self._tracker = None
            tracker = None
        if state.game_over:
            if tracker is not None:
                review = tracker.finish(state)
                self._tracker = None
                self._pending.append(review)
                self.reviews.append(review)
                # Background watching can run for days without anyone draining
                # the queue, and every review is already on disk.
                del self._pending[:-MAX_KEPT_REVIEWS]
                del self.reviews[:-MAX_KEPT_REVIEWS]
                _persist(review)
            return
        if not state.in_match or state.turn_number is None:
            return
        if tracker is None:
            tracker = self._tracker = _GameTracker(state)
        tracker.observe(state)

    def pop_pending(self) -> list[GameReview]:
        """Hand the UI every review filed since it last asked."""
        with self._lock:
            ready, self._pending = self._pending, []
        return ready

    def latest(self) -> GameReview | None:
        with self._lock:
            return self.reviews[-1] if self.reviews else None

    def match_reviews(self) -> list[GameReview]:
        """Every game of the most recent match, oldest first."""
        with self._lock:
            if not self.reviews:
                return []
            match_id = self.reviews[-1].match_id
            return [review for review in self.reviews if review.match_id == match_id]


def _same_game(tracker: _GameTracker, state: ArenaGameState) -> bool:
    if state.match_id and tracker.match_id and state.match_id != tracker.match_id:
        return False
    return not (state.game_number and state.game_number != tracker.game_number)


def _persist(review: GameReview) -> None:
    """Keep a durable copy so a review survives closing the companion."""
    try:
        REVIEW_DIR.mkdir(parents=True, exist_ok=True)
        stamp = hashlib.sha256(review.match_id.encode()).hexdigest()[:16] if review.match_id else datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = REVIEW_DIR / f"{stamp}-game{review.game_number}.md"
        path.write_text(review.to_markdown(), encoding="utf-8")
    except OSError:
        # A read-only or full disk must not cost the user the on-screen review.
        pass


REVIEWER = MatchReviewer()


def install_reviewer() -> MatchReviewer:
    """Start feeding the shared Arena log reader into the reviewer."""
    set_state_observer(REVIEWER.observe)
    return REVIEWER


def render_last_match() -> str:
    """Plain-text review of the match just finished, for the chat pane."""
    reviews = REVIEWER.match_reviews()
    if not reviews:
        return (
            "I have not seen a game finish yet. Leave the companion running through a "
            "match and I will break down every decision the moment the game ends.\n\n"
            "Reviews are also written to data/match-reviews/ so you can read them later."
        )
    if len(reviews) == 1:
        return reviews[0].render()
    blocks = [f"Match review — {len(reviews)} games", ""]
    for review in reviews:
        blocks.extend([review.render(), "", "-" * 48, ""])
    return "\n".join(blocks).rstrip("- \n")
