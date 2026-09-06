"""Arena reading path.

Arena publishes no UI Automation tree and its board defeats OCR, so everything
the companion knows about an Arena game comes from the client's own detailed
log. These tests drive that path end to end with synthetic engine records in
the exact shape Arena writes.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mtg_companion.actions import classify_arena_actions
from mtg_companion.arena_state import ArenaLogReader, render_state
from mtg_companion.offline import answer_question, explain_visible_state, screen_text_matches_client


class _StubCardDatabase:
    """Stands in for Arena's installed card database inside tests."""

    NAMES = {75450: "Hallowed Priest", 75452: "Inspiring Commander", 90001: "Bazaar Trader"}
    TYPES = {
        75450: "Creature - Human Cleric",
        75452: "Creature - Human Soldier",
        90001: "Creature - Goblin",
    }

    def localized(self, loc_id):
        return ""

    def card_name(self, grp_id):
        return self.NAMES.get(grp_id, "")

    def type_line(self, grp_id):
        return self.TYPES.get(grp_id, "")

    def ability_text(self, ability_grp_id):
        return ""

    def prompt_text(self, prompt_id):
        return {7: "Choose blockers."}.get(prompt_id, "")


def _gre_line(*messages):
    """One log record in Arena's shape: a header line, then the JSON payload."""
    payload = json.dumps({"greToClientEvent": {"greToClientMessages": list(messages)}})
    return "[UnityCrossThreadLogger]Match to X: GreToClientEvent\n" + payload


def _full_state_message():
    return {
        "type": "GREMessageType_GameStateMessage",
        "systemSeatIds": [1],
        "gameStateMessage": {
            "type": "GameStateType_Full",
            "gameInfo": {"stage": "GameStage_Play", "matchState": "MatchState_GameInProgress"},
            "players": [
                {"systemSeatNumber": 1, "lifeTotal": 6, "teamId": 1},
                {"systemSeatNumber": 2, "lifeTotal": 7, "teamId": 2},
            ],
            "turnInfo": {
                "phase": "Phase_Combat",
                "step": "Step_DeclareBlock",
                "turnNumber": 17,
                "activePlayer": 2,
                "priorityPlayer": 2,
                "decisionPlayer": 1,
            },
            "zones": [
                {"zoneId": 28, "type": "ZoneType_Battlefield"},
                {"zoneId": 31, "type": "ZoneType_Hand", "ownerSeatId": 1, "objectInstanceIds": [42]},
                {
                    "zoneId": 32,
                    "type": "ZoneType_Library",
                    "ownerSeatId": 1,
                    "objectInstanceIds": list(range(300, 344)),
                },
                {
                    "zoneId": 33,
                    "type": "ZoneType_Graveyard",
                    "ownerSeatId": 1,
                    "objectInstanceIds": [1, 2, 3, 4, 5],
                },
                {"zoneId": 35, "type": "ZoneType_Hand", "ownerSeatId": 2, "objectInstanceIds": [99]},
            ],
            "gameObjects": [
                {"instanceId": 42, "grpId": 75452, "zoneId": 31, "ownerSeatId": 1, "controllerSeatId": 1},
                {
                    "instanceId": 51,
                    "grpId": 75450,
                    "zoneId": 28,
                    "ownerSeatId": 1,
                    "controllerSeatId": 1,
                    "power": {"value": 2},
                    "toughness": {"value": 2},
                },
                {
                    "instanceId": 60,
                    "grpId": 75450,
                    "zoneId": 28,
                    "ownerSeatId": 2,
                    "controllerSeatId": 2,
                    "power": {"value": 4},
                    "toughness": {"value": 4},
                    "isTapped": True,
                    "attackState": "AttackState_Attacking",
                },
            ],
            "actions": [
                {
                    "seatId": 1,
                    "action": {
                        "actionType": "ActionType_Cast",
                        "instanceId": 42,
                        "grpId": 75452,
                        "manaCost": [
                            {"color": ["ManaColor_Generic"], "count": 4},
                            {"color": ["ManaColor_White"], "count": 2},
                        ],
                    },
                },
                {"seatId": 2, "action": {"actionType": "ActionType_Cast", "instanceId": 99, "grpId": 75450}},
            ],
        },
    }


def _blockers_request():
    return {
        "type": "GREMessageType_DeclareBlockersReq",
        "systemSeatIds": [1],
        "prompt": {"promptId": 7},
        "declareBlockersReq": {"blockers": [{"blockerInstanceId": 51, "attackerInstanceIds": [60]}]},
    }


def _write_log(directory, *lines):
    log_path = Path(directory) / "Player.log"
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


class _ArenaTestCase(unittest.TestCase):
    def setUp(self):
        patcher = patch("mtg_companion.arena_state.load_card_database", return_value=_StubCardDatabase())
        patcher.start()
        self.addCleanup(patcher.stop)

    def read_state(self, *lines):
        with tempfile.TemporaryDirectory() as directory:
            log_path = _write_log(directory, *lines)
            with patch("mtg_companion.arena_state.find_player_log", return_value=log_path):
                return ArenaLogReader().poll()

    def live_state(self, *messages):
        return self.read_state(_gre_line(*messages))


class ArenaStateTests(_ArenaTestCase):
    def test_board_is_rebuilt_from_the_engine_records(self):
        state = self.live_state(_full_state_message(), _blockers_request())
        self.assertTrue(state.detailed)
        self.assertTrue(state.in_match)
        self.assertEqual(state.my_seat, 1)
        self.assertEqual((state.my_life, state.opponent_life), (6, 7))
        self.assertEqual(state.turn_number, 17)
        self.assertFalse(state.my_turn)
        self.assertTrue(state.waiting_on_me)
        self.assertEqual(state.request, "Declare blockers")
        self.assertEqual(state.prompt, "Choose blockers.")
        self.assertEqual([card.name for card in state.hand], ["Inspiring Commander"])
        self.assertEqual([card.name for card in state.my_battlefield], ["Hallowed Priest"])
        self.assertTrue(state.opponent_battlefield[0].attacking)
        self.assertEqual(state.my_library, 44)
        self.assertEqual(state.my_graveyard, 5)
        self.assertEqual(state.opponent_hand, 1)

    def test_rendered_board_names_both_sides(self):
        report = render_state(self.live_state(_full_state_message(), _blockers_request()))
        self.assertIn("Turn 17", report)
        self.assertIn("Hallowed Priest (4/4)", report)
        self.assertIn("attacking", report)

    def test_opponent_actions_are_never_reported_as_yours(self):
        state = self.live_state(_full_state_message())
        self.assertEqual([action.card for action in state.actions], ["Inspiring Commander"])
        self.assertIn("{4}{W}{W}", state.actions[0].label())

    def test_a_diff_replaces_the_object_it_resends(self):
        diff = {
            "type": "GREMessageType_GameStateMessage",
            "systemSeatIds": [1],
            "gameStateMessage": {
                "type": "GameStateType_Diff",
                "turnInfo": {
                    "phase": "Phase_Main2",
                    "step": "",
                    "turnNumber": 18,
                    "activePlayer": 1,
                    "priorityPlayer": 1,
                    "decisionPlayer": 1,
                },
                "players": [{"systemSeatNumber": 1, "lifeTotal": 2}],
                "gameObjects": [
                    {
                        "instanceId": 60,
                        "grpId": 75450,
                        "zoneId": 28,
                        "ownerSeatId": 2,
                        "controllerSeatId": 2,
                        "power": {"value": 4},
                        "toughness": {"value": 4},
                    }
                ],
            },
        }
        state = self.read_state(_gre_line(_full_state_message()), _gre_line(diff))
        self.assertEqual(state.turn_number, 18)
        self.assertEqual(state.my_life, 2)
        self.assertTrue(state.my_turn)
        # The attacker was resent without attackState, so combat must be over.
        self.assertFalse(state.opponent_battlefield[0].attacking)

    def test_a_finished_match_is_not_reported_as_a_live_board(self):
        message = _full_state_message()
        message["gameStateMessage"]["gameInfo"] = {
            "stage": "GameStage_GameOver",
            "matchState": "MatchState_MatchComplete",
        }
        state = self.live_state(message)
        self.assertFalse(state.in_match)
        self.assertTrue(state.game_over)

    def test_a_new_game_is_not_poisoned_by_the_previous_result(self):
        """The finished match's result used to survive the next snapshot, so a
        live board was reported as "no game in progress"."""
        finished = _full_state_message()
        finished["gameStateMessage"]["gameInfo"] = {
            "stage": "GameStage_GameOver",
            "matchState": "MatchState_MatchComplete",
        }
        completed = json.dumps(
            {
                "matchGameRoomStateChangedEvent": {
                    "gameRoomInfo": {
                        "gameRoomConfig": {
                            "reservedPlayers": [
                                {"systemSeatId": 1, "playerName": "doclath"},
                                {"systemSeatId": 2, "playerName": "Chandra Nalaar"},
                            ]
                        },
                        "stateType": "MatchGameRoomStateType_MatchCompleted",
                        "finalMatchResult": {
                            "resultList": [{"scope": "MatchScope_Match", "winningTeamId": 2}]
                        },
                    }
                }
            }
        )
        state = self.read_state(
            _gre_line(finished),
            "[UnityCrossThreadLogger]Match to X: MatchGameRoomStateChangedEvent\n" + completed,
            _gre_line(_full_state_message(), _blockers_request()),
        )
        self.assertTrue(state.in_match)
        self.assertFalse(state.game_over)
        self.assertEqual(state.result, "")
        self.assertEqual(state.turn_number, 17)
        self.assertEqual(state.opponent_name, "Chandra Nalaar")

    def test_detailed_logs_off_explains_the_exact_fix(self):
        state = self.read_state("[UnityCrossThreadLogger]Client connected", "nothing structured here")
        self.assertTrue(state.available)
        self.assertFalse(state.detailed)
        self.assertIn("Detailed Logs", state.reason)

    def test_new_lines_are_picked_up_without_rereading_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = _write_log(directory, _gre_line(_full_state_message()))
            reader = ArenaLogReader()
            with patch("mtg_companion.arena_state.find_player_log", return_value=log_path):
                first = reader.poll()
                self.assertEqual(first.turn_number, 17)
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(_gre_line(_blockers_request()) + "\n")
                second = reader.poll()
            self.assertEqual(second.request, "Declare blockers")


class ArenaGuidanceTests(_ArenaTestCase):
    def test_arena_board_beats_an_unreadable_frame(self):
        state = self.live_state(_full_state_message(), _blockers_request())
        result = explain_visible_state("MTGA", "[Click to Continue]", arena=state)
        self.assertIn("turn 17", result.heading.casefold())
        self.assertIn("declare blockers", result.summary.casefold())
        self.assertIn("Hallowed Priest", result.evidence)

    def test_almost_empty_arena_ocr_is_not_rejected(self):
        # OCR reads roughly thirty characters off a full Arena battlefield, so
        # the old minimum-length rule threw every board away.
        self.assertTrue(screen_text_matches_client("MTGA", "ie\n\n[Click to Continue]"))
        self.assertFalse(screen_text_matches_client("MTGA", "Android Studio Gradle build"))

    def test_a_card_name_containing_a_currency_word_is_still_allowed(self):
        message = _full_state_message()
        message["gameStateMessage"]["actions"] = [
            {"seatId": 1, "action": {"actionType": "ActionType_Cast", "instanceId": 90001, "grpId": 90001}}
        ]
        state = self.live_state(message)
        permitted = classify_arena_actions(state)
        self.assertIn("Bazaar Trader", " ".join(label for label, _ in permitted.allowed))
        self.assertNotIn("Bazaar Trader", " ".join(label for label, _ in permitted.blocked))

    def test_conceding_is_blocked_in_arena_too(self):
        permitted = classify_arena_actions(self.live_state(_full_state_message(), _blockers_request()))
        self.assertIn("Concede", [label for label, _ in permitted.blocked])
        self.assertEqual(permitted.recommended, "Assign blockers")

    def test_every_request_recommends_one_concrete_move(self):
        """A player who does not yet know the options needs to be shown a real
        move, not just told the decision is theirs. Each request names one."""
        message = _full_state_message()
        message["gameStateMessage"]["turnInfo"].update({"activePlayer": 1, "decisionPlayer": 1})
        expected = {
            "GREMessageType_MulliganReq": "Keep",
            "GREMessageType_DeclareAttackersReq": "Attack with all",
            "GREMessageType_ActionsAvailableReq": "Pass priority",
            "GREMessageType_PayCostsReq": "Accept the auto-tap",
        }
        for request_type, move in expected.items():
            request = {"type": request_type, "systemSeatIds": [1], "prompt": {"promptId": 0}}
            state = self.live_state(message, request)
            self.assertEqual(classify_arena_actions(state).recommended, move, request_type)

    def test_a_recommended_judgement_call_still_says_it_is_yours(self):
        message = _full_state_message()
        message["gameStateMessage"]["turnInfo"].update({"activePlayer": 1, "decisionPlayer": 1})
        request = {"type": "GREMessageType_MulliganReq", "systemSeatIds": [1], "prompt": {"promptId": 0}}
        permitted = classify_arena_actions(self.live_state(message, request))
        notes = dict(permitted.allowed)
        self.assertEqual(permitted.recommended, "Keep")
        self.assertIn("Mulligan", notes)
        self.assertIn("bottom", notes["Keep"].casefold())

    def test_next_click_names_the_real_attackers(self):
        state = self.live_state(_full_state_message(), _blockers_request())
        answer = answer_question("What do I click next?", "MTGA", "", "", state)
        self.assertIn("Hallowed Priest", answer)
        self.assertIn("blocker", answer.casefold())

    def test_waiting_on_the_opponent_says_do_not_click(self):
        message = _full_state_message()
        message["gameStateMessage"]["turnInfo"]["decisionPlayer"] = 2
        message["gameStateMessage"]["turnInfo"]["priorityPlayer"] = 2
        state = self.live_state(message)
        answer = answer_question("What do I click next?", "MTGA", "", "", state)
        self.assertIn("waiting on your opponent", answer.casefold())

    def test_no_game_in_progress_points_at_the_play_button(self):
        state = self.read_state("[UnityCrossThreadLogger]Client connected")
        answer = answer_question("What do I click next?", "MTGA", "", "", state)
        self.assertIn("Detailed Logs", answer)


if __name__ == "__main__":
    unittest.main()
