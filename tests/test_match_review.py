"""Post-match review.

Every judgement the reviewer makes has to come from Arena's own engine records,
so these tests drive it with engine messages in the exact shape Arena writes and
assert on the coaching that comes out the other end.

The synthetic games below are deliberately small: one decision each, with the
board arranged so that exactly one verdict is correct.
"""

import json
import unittest
from unittest.mock import patch

from mtg_companion.arena_state import _Accumulator
from mtg_companion.match_review import (
    BLUNDER,
    GOOD,
    MISTAKE,
    NOTE,
    MatchReviewer,
)

# grpIds used below. Abilities are keyed the way Arena keys them: the object
# carries {"id": ordinal, "grpId": ability}, and the ability's text is what the
# reviewer reasons about.
BEAR = 1001            # 2/2 vanilla
GIANT = 1002           # 3/3 vanilla
WALL = 1003            # 0/4 vanilla
DRAKE = 1004           # 2/2 flier
TRICKSTER = 1005       # 2/2 with text the reviewer must refuse to reason past
PLAINS = 1006
SPELL = 1007

FLYING = 8
LIFELINK = 12


class _StubCardDatabase:
    NAMES = {
        BEAR: "Grizzly Bears",
        GIANT: "Hill Giant",
        WALL: "Stone Wall",
        DRAKE: "Storm Drake",
        TRICKSTER: "Sneaky Trickster",
        PLAINS: "Plains",
        SPELL: "Angelic Reward",
    }
    ABILITIES = {
        FLYING: "Flying",
        LIFELINK: "Lifelink",
        9001: "Whenever CARDNAME becomes blocked, it gets +3/+3 until end of turn.",
        9002: "When CARDNAME enters, draw a card.",
    }

    def localized(self, loc_id):
        return ""

    def card_name(self, grp_id):
        return self.NAMES.get(grp_id, "")

    def type_line(self, grp_id):
        return "Land" if grp_id == PLAINS else "Creature"

    def ability_text(self, ability_grp_id):
        return self.ABILITIES.get(ability_grp_id, "")

    def prompt_text(self, prompt_id):
        return ""


def _creature(instance, grp, seat, power, toughness, **extra):
    obj = {
        "instanceId": instance,
        "grpId": grp,
        "zoneId": 28,
        "ownerSeatId": seat,
        "controllerSeatId": seat,
        "cardTypes": ["CardType_Creature"],
        "power": {"value": power},
        "toughness": {"value": toughness},
    }
    obj.update(extra)
    return obj


def _abilities(*grp_ids):
    return [{"id": index, "grpId": grp} for index, grp in enumerate(grp_ids, start=1)]


def _state(turn, phase, step, objects, *, active=1, decision=1, my_life=20,
           their_life=20, hand=(), full=False, actions=(), zones=None, deleted=()):
    message = {
        "type": "GREMessageType_GameStateMessage",
        "systemSeatIds": [1],
        "gameStateMessage": {
            "type": "GameStateType_Full" if full else "GameStateType_Diff",
            "gameInfo": {
                "matchID": "test-match",
                "gameNumber": 1,
                "stage": "GameStage_Play",
                "matchState": "MatchState_GameInProgress",
            },
            "players": [
                {"systemSeatNumber": 1, "lifeTotal": my_life, "teamId": 1},
                {"systemSeatNumber": 2, "lifeTotal": their_life, "teamId": 2},
            ],
            "turnInfo": {
                "turnNumber": turn,
                "phase": phase,
                "step": step,
                "activePlayer": active,
                "priorityPlayer": decision,
                "decisionPlayer": decision,
            },
            "zones": zones
            if zones is not None
            else [
                {"zoneId": 28, "type": "ZoneType_Battlefield"},
                {
                    "zoneId": 31,
                    "type": "ZoneType_Hand",
                    "ownerSeatId": 1,
                    "objectInstanceIds": [card["instanceId"] for card in hand],
                },
            ],
            "gameObjects": list(objects) + list(hand),
            "actions": list(actions),
            # Arena reports a permanent leaving the battlefield by id, not by
            # dropping it from the object list, so the tests must do the same.
            "diffDeletedInstanceIds": list(deleted),
        },
    }
    return message


def _game_over(won=True, objects=(), my_life=20, their_life=0, hand=()):
    return {
        "type": "GREMessageType_GameStateMessage",
        "systemSeatIds": [1],
        "gameStateMessage": {
            "type": "GameStateType_Diff",
            "gameInfo": {
                "matchID": "test-match",
                "gameNumber": 1,
                "stage": "GameStage_GameOver",
                "matchState": "MatchState_GameComplete",
                "results": [
                    {
                        "scope": "MatchScope_Game",
                        "result": "ResultType_WinLoss",
                        "winningTeamId": 1 if won else 2,
                        "reason": "ResultReason_Game",
                    }
                ],
            },
            "players": [
                {"systemSeatNumber": 1, "lifeTotal": my_life, "teamId": 1},
                {"systemSeatNumber": 2, "lifeTotal": their_life, "teamId": 2},
            ],
            "turnInfo": {"turnNumber": 9, "phase": "Phase_Ending", "step": "Step_End"},
            "zones": [
                {"zoneId": 28, "type": "ZoneType_Battlefield"},
                {
                    "zoneId": 31,
                    "type": "ZoneType_Hand",
                    "ownerSeatId": 1,
                    "objectInstanceIds": [card["instanceId"] for card in hand],
                },
            ],
            "gameObjects": list(objects) + list(hand),
        },
    }


def _run(messages):
    """Feed engine messages through the real reader and return the review."""
    reviewer = MatchReviewer()
    accumulator = _Accumulator(reviewer.observe)
    for message in messages:
        payload = json.dumps({"greToClientEvent": {"greToClientMessages": [message]}})
        accumulator.feed_line("[UnityCrossThreadLogger]Match to X: GreToClientEvent\n" + payload)
    reviews = reviewer.reviews
    assert reviews, "the game never produced a review"
    return reviews[-1]


def _titles(review, grade):
    return [note.title for note in review.notes if note.grade == grade]


class MatchReviewTests(unittest.TestCase):
    def setUp(self):
        patcher = patch(
            "mtg_companion.arena_state.load_card_database", return_value=_StubCardDatabase()
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # Reviews are written to disk in normal use; tests must not litter it.
        persist = patch("mtg_companion.match_review._persist")
        persist.start()
        self.addCleanup(persist.stop)

    # ---- combat the reviewer must praise -------------------------------

    def test_unopposed_attack_is_reported_as_a_good_move(self):
        mine = _creature(10, BEAR, 1, 2, 2)
        attacking = _creature(10, BEAR, 1, 2, 2, attackState="AttackState_Attacking", isTapped=True)
        review = _run(
            [
                _state(3, "Phase_Main1", "", [mine], full=True),
                _state(3, "Phase_Combat", "Step_DeclareAttack", [mine]),
                _state(3, "Phase_Combat", "Step_DeclareAttack", [attacking]),
                _state(3, "Phase_Combat", "Step_CombatDamage", [attacking], their_life=18),
                _state(3, "Phase_Main2", "", [attacking], their_life=18),
                _game_over(won=True, objects=[mine], their_life=18),
            ]
        )
        self.assertIn(
            "Attack connected for 2 and cost you nothing", _titles(review, GOOD)
        )

    def test_clean_block_is_reported_as_a_good_move(self):
        their_attacker = _creature(
            20, BEAR, 2, 2, 2, attackState="AttackState_Attacking", isTapped=True
        )
        my_giant = _creature(10, GIANT, 1, 3, 3)
        blocking = _creature(
            10, GIANT, 1, 3, 3, blockState="BlockState_Blocking", blockInfo={"attackerIds": [20]}
        )
        review = _run(
            [
                _state(4, "Phase_Main1", "", [my_giant], active=2, decision=2, full=True),
                _state(4, "Phase_Combat", "Step_DeclareAttack", [my_giant, their_attacker], active=2, decision=2),
                _state(4, "Phase_Combat", "Step_DeclareBlock", [my_giant, their_attacker], active=2),
                _state(4, "Phase_Combat", "Step_DeclareBlock", [blocking, their_attacker], active=2),
                # The attacker dies, the blocker lives.
                _state(4, "Phase_Combat", "Step_CombatDamage", [my_giant], active=2, deleted=[20]),
                _state(4, "Phase_Main2", "", [my_giant], active=2),
                _game_over(won=True, objects=[my_giant]),
            ]
        )
        self.assertTrue(
            any("Clean block" in title for title in _titles(review, GOOD)),
            _titles(review, GOOD),
        )

    # ---- combat the reviewer must criticise ----------------------------

    def test_a_free_block_that_was_not_taken_is_a_mistake(self):
        their_attacker = _creature(
            20, BEAR, 2, 2, 2, attackState="AttackState_Attacking", isTapped=True
        )
        my_giant = _creature(10, GIANT, 1, 3, 3)
        review = _run(
            [
                _state(4, "Phase_Main1", "", [my_giant], active=2, decision=2, full=True),
                _state(4, "Phase_Combat", "Step_DeclareAttack", [my_giant, their_attacker], active=2, decision=2),
                _state(4, "Phase_Combat", "Step_DeclareBlock", [my_giant, their_attacker], active=2),
                _state(
                    4, "Phase_Combat", "Step_CombatDamage",
                    [my_giant, their_attacker], active=2, my_life=18,
                ),
                _state(4, "Phase_Main2", "", [my_giant, their_attacker], active=2, my_life=18),
                _game_over(won=False, objects=[my_giant, their_attacker], my_life=18, their_life=20),
            ]
        )
        self.assertTrue(
            any("Free block available" in title for title in _titles(review, MISTAKE)),
            _titles(review, MISTAKE),
        )

    def test_blocking_with_the_wrong_creature_is_a_mistake(self):
        their_attacker = _creature(
            20, BEAR, 2, 2, 2, attackState="AttackState_Attacking", isTapped=True
        )
        my_giant = _creature(10, GIANT, 1, 3, 3)
        my_chump = _creature(11, BEAR, 1, 1, 1)
        chumping = _creature(
            11, BEAR, 1, 1, 1, blockState="BlockState_Blocking", blockInfo={"attackerIds": [20]}
        )
        review = _run(
            [
                _state(4, "Phase_Main1", "", [my_giant, my_chump], active=2, decision=2, full=True),
                _state(4, "Phase_Combat", "Step_DeclareAttack", [my_giant, my_chump, their_attacker], active=2, decision=2),
                _state(4, "Phase_Combat", "Step_DeclareBlock", [my_giant, my_chump, their_attacker], active=2),
                _state(4, "Phase_Combat", "Step_DeclareBlock", [my_giant, chumping, their_attacker], active=2),
                # The 1/1 dies; the 2/2 attacker survives; the 3/3 never blocked.
                _state(4, "Phase_Combat", "Step_CombatDamage", [my_giant, their_attacker], active=2, deleted=[11]),
                _state(4, "Phase_Main2", "", [my_giant, their_attacker], active=2),
                _game_over(won=False, objects=[my_giant, their_attacker], their_life=20),
            ]
        )
        self.assertTrue(
            any("with the wrong creature" in title for title in _titles(review, MISTAKE)),
            _titles(review, MISTAKE),
        )

    def test_lethal_on_an_empty_board_is_a_blunder(self):
        mine = [_creature(10 + n, GIANT, 1, 3, 3) for n in range(3)]
        attacking_one = _creature(
            10, GIANT, 1, 3, 3, attackState="AttackState_Attacking", isTapped=True
        )
        rest = mine[1:]
        review = _run(
            [
                _state(7, "Phase_Main1", "", mine, their_life=8, full=True),
                _state(7, "Phase_Combat", "Step_DeclareAttack", mine, their_life=8),
                _state(7, "Phase_Combat", "Step_DeclareAttack", [attacking_one] + rest, their_life=8),
                _state(7, "Phase_Combat", "Step_CombatDamage", [attacking_one] + rest, their_life=5),
                _state(7, "Phase_Main2", "", [attacking_one] + rest, their_life=5),
                _game_over(won=False, objects=mine, their_life=5),
            ]
        )
        self.assertIn(
            "You had lethal on board and did not take it", _titles(review, BLUNDER)
        )

    # ---- claims the reviewer must refuse to make -----------------------

    def test_no_block_advice_when_the_attacker_has_a_combat_trick(self):
        """Sneaky Trickster gets +3/+3 when blocked, so the 3/3 does not win."""
        trickster = _creature(
            20, TRICKSTER, 2, 2, 2,
            attackState="AttackState_Attacking",
            isTapped=True,
            uniqueAbilities=_abilities(9001),
        )
        my_giant = _creature(10, GIANT, 1, 3, 3)
        review = _run(
            [
                _state(4, "Phase_Main1", "", [my_giant], active=2, decision=2, full=True),
                _state(4, "Phase_Combat", "Step_DeclareAttack", [my_giant, trickster], active=2, decision=2),
                _state(4, "Phase_Combat", "Step_DeclareBlock", [my_giant, trickster], active=2),
                _state(4, "Phase_Combat", "Step_CombatDamage", [my_giant, trickster], active=2, my_life=18),
                _state(4, "Phase_Main2", "", [my_giant, trickster], active=2, my_life=18),
                _game_over(won=False, objects=[my_giant, trickster], my_life=18, their_life=20),
            ]
        )
        self.assertEqual(
            [], [t for t in _titles(review, MISTAKE) if "Free block" in t]
        )

    def test_a_ground_creature_is_never_told_to_block_a_flier(self):
        flier = _creature(
            20, DRAKE, 2, 2, 2,
            attackState="AttackState_Attacking",
            isTapped=True,
            uniqueAbilities=_abilities(FLYING),
        )
        my_giant = _creature(10, GIANT, 1, 3, 3)
        review = _run(
            [
                _state(4, "Phase_Main1", "", [my_giant], active=2, decision=2, full=True),
                _state(4, "Phase_Combat", "Step_DeclareAttack", [my_giant, flier], active=2, decision=2),
                _state(4, "Phase_Combat", "Step_DeclareBlock", [my_giant, flier], active=2),
                _state(4, "Phase_Combat", "Step_CombatDamage", [my_giant, flier], active=2, my_life=18),
                _state(4, "Phase_Main2", "", [my_giant, flier], active=2, my_life=18),
                _game_over(won=False, objects=[my_giant, flier], my_life=18, their_life=20),
            ]
        )
        self.assertEqual(
            [], [t for t in _titles(review, MISTAKE) if "Free block" in t]
        )

    def test_holding_a_creature_back_is_not_criticised_when_they_can_block(self):
        mine = [_creature(10 + n, GIANT, 1, 3, 3) for n in range(3)]
        theirs = _creature(20, WALL, 2, 0, 4)
        attacking_one = _creature(
            10, GIANT, 1, 3, 3, attackState="AttackState_Attacking", isTapped=True
        )
        review = _run(
            [
                _state(7, "Phase_Main1", "", mine + [theirs], their_life=8, full=True),
                _state(7, "Phase_Combat", "Step_DeclareAttack", mine + [theirs], their_life=8),
                _state(7, "Phase_Combat", "Step_DeclareAttack", [attacking_one] + mine[1:] + [theirs], their_life=8),
                _state(7, "Phase_Main2", "", [attacking_one] + mine[1:] + [theirs], their_life=8),
                _game_over(won=False, objects=mine + [theirs], their_life=8),
            ]
        )
        self.assertEqual([], _titles(review, BLUNDER))

    # ---- resource use --------------------------------------------------

    def test_missed_land_drop_is_a_blunder(self):
        land_in_hand = {
            "instanceId": 50,
            "grpId": PLAINS,
            "zoneId": 31,
            "ownerSeatId": 1,
            "controllerSeatId": 1,
            "cardTypes": ["CardType_Land"],
        }
        play_land = {
            "seatId": 1,
            "action": {"actionType": "ActionType_Play", "instanceId": 50, "grpId": PLAINS},
        }
        review = _run(
            [
                _state(3, "Phase_Main1", "", [], hand=[land_in_hand], actions=[play_land], full=True),
                _state(3, "Phase_Main2", "", [], hand=[land_in_hand], actions=[play_land]),
                _state(3, "Phase_Ending", "Step_End", [], hand=[land_in_hand], actions=[play_land]),
                _state(4, "Phase_Main1", "", [], active=2, decision=2, hand=[land_in_hand]),
                _game_over(won=False, hand=[land_in_hand], their_life=20),
            ]
        )
        self.assertIn("Missed your land drop", _titles(review, BLUNDER))

    def test_an_affordable_uncast_spell_is_a_mistake(self):
        spell_in_hand = {
            "instanceId": 51,
            "grpId": SPELL,
            "zoneId": 31,
            "ownerSeatId": 1,
            "controllerSeatId": 1,
            "cardTypes": ["CardType_Creature"],
        }
        land = _creature(60, PLAINS, 1, 0, 0)
        land["cardTypes"] = ["CardType_Land"]
        del land["power"], land["toughness"]
        cast = {
            "seatId": 1,
            "action": {
                "actionType": "ActionType_Cast",
                "instanceId": 51,
                "grpId": SPELL,
                "manaCost": [{"color": ["ManaColor_White"], "count": 1}],
            },
        }
        mana = {
            "seatId": 1,
            "action": {"actionType": "ActionType_Activate_Mana", "instanceId": 60},
        }
        review = _run(
            [
                _state(3, "Phase_Main1", "", [land], hand=[spell_in_hand], actions=[cast, mana], full=True),
                _state(3, "Phase_Main2", "", [land], hand=[spell_in_hand], actions=[cast, mana]),
                _state(3, "Phase_Ending", "Step_End", [land], hand=[spell_in_hand], actions=[cast, mana]),
                _state(4, "Phase_Main1", "", [land], active=2, decision=2, hand=[spell_in_hand]),
                _game_over(won=False, objects=[land], hand=[spell_in_hand], their_life=20),
            ]
        )
        self.assertTrue(
            any("unplayed" in title for title in _titles(review, MISTAKE)),
            _titles(review, MISTAKE),
        )

    def test_a_spell_you_could_not_afford_is_never_called_a_mistake(self):
        spell_in_hand = {
            "instanceId": 51,
            "grpId": SPELL,
            "zoneId": 31,
            "ownerSeatId": 1,
            "controllerSeatId": 1,
            "cardTypes": ["CardType_Creature"],
        }
        # Arena offers a cast whenever the timing is legal, affordable or not.
        cast = {
            "seatId": 1,
            "action": {
                "actionType": "ActionType_Cast",
                "instanceId": 51,
                "grpId": SPELL,
                "manaCost": [
                    {"color": ["ManaColor_Generic"], "count": 4},
                    {"color": ["ManaColor_White"], "count": 2},
                ],
            },
        }
        review = _run(
            [
                _state(3, "Phase_Main1", "", [], hand=[spell_in_hand], actions=[cast], full=True),
                _state(3, "Phase_Ending", "Step_End", [], hand=[spell_in_hand], actions=[cast]),
                _state(4, "Phase_Main1", "", [], active=2, decision=2, hand=[spell_in_hand]),
                _game_over(won=False, hand=[spell_in_hand], their_life=20),
            ]
        )
        self.assertEqual(
            [], [t for t in _titles(review, MISTAKE) if "unplayed" in t]
        )

    def test_an_instant_held_up_is_never_called_a_mistake(self):
        instant_in_hand = {
            "instanceId": 52,
            "grpId": SPELL,
            "zoneId": 31,
            "ownerSeatId": 1,
            "controllerSeatId": 1,
            "cardTypes": ["CardType_Instant"],
        }
        land = _creature(60, PLAINS, 1, 0, 0)
        land["cardTypes"] = ["CardType_Land"]
        del land["power"], land["toughness"]
        cast = {
            "seatId": 1,
            "action": {
                "actionType": "ActionType_Cast",
                "instanceId": 52,
                "grpId": SPELL,
                "manaCost": [{"color": ["ManaColor_White"], "count": 1}],
            },
        }
        mana = {
            "seatId": 1,
            "action": {"actionType": "ActionType_Activate_Mana", "instanceId": 60},
        }
        review = _run(
            [
                _state(3, "Phase_Main1", "", [land], hand=[instant_in_hand], actions=[cast, mana], full=True),
                _state(3, "Phase_Ending", "Step_End", [land], hand=[instant_in_hand], actions=[cast, mana]),
                _state(4, "Phase_Main1", "", [land], active=2, decision=2, hand=[instant_in_hand]),
                _game_over(won=False, objects=[land], hand=[instant_in_hand], their_life=20),
            ]
        )
        self.assertEqual([], [t for t in _titles(review, MISTAKE) if "unplayed" in t])

    # ---- shape of the report -------------------------------------------

    def test_the_report_names_the_result_and_survives_rendering(self):
        mine = _creature(10, BEAR, 1, 2, 2)
        review = _run(
            [
                _state(3, "Phase_Main1", "", [mine], full=True),
                _game_over(won=True, objects=[mine], their_life=0),
            ]
        )
        self.assertEqual("You won this game.", review.result)
        self.assertIn("You won this game", review.render())
        self.assertIn("#", review.to_markdown())
        self.assertIn("You won this game", _titles(review, GOOD))

    def test_a_finished_game_is_handed_over_exactly_once(self):
        reviewer = MatchReviewer()
        accumulator = _Accumulator(reviewer.observe)
        mine = _creature(10, BEAR, 1, 2, 2)
        for message in (_state(3, "Phase_Main1", "", [mine], full=True), _game_over(True, [mine])):
            payload = json.dumps({"greToClientEvent": {"greToClientMessages": [message]}})
            accumulator.feed_line("[UnityCrossThreadLogger]X\n" + payload)
        with patch("mtg_companion.match_review._persist"):
            self.assertEqual(1, len(reviewer.pop_pending()))
            self.assertEqual([], reviewer.pop_pending())

    def test_keeping_a_one_land_hand_is_worth_knowing(self):
        hand = [
            {
                "instanceId": 70,
                "grpId": PLAINS,
                "zoneId": 31,
                "ownerSeatId": 1,
                "controllerSeatId": 1,
                "cardTypes": ["CardType_Land"],
            }
        ] + [
            {
                "instanceId": 71 + n,
                "grpId": BEAR,
                "zoneId": 31,
                "ownerSeatId": 1,
                "controllerSeatId": 1,
                "cardTypes": ["CardType_Creature"],
            }
            for n in range(6)
        ]
        mulligan = {
            "type": "GREMessageType_MulliganReq",
            "systemSeatIds": [1],
            "mulliganReq": {},
        }
        review = _run(
            [
                _state(1, "Phase_Beginning", "Step_Upkeep", [], hand=hand, full=True),
                mulligan,
                _state(1, "Phase_Main1", "", [], hand=hand),
                _game_over(won=False, hand=hand, their_life=20),
            ]
        )
        self.assertTrue(
            any("with 1 land" in title for title in _titles(review, NOTE)),
            _titles(review, NOTE),
        )


if __name__ == "__main__":
    unittest.main()
