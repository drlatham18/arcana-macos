import unittest
from dataclasses import replace
from compact import analyze
from mtg_companion.arena_state import ArenaGameState as G, ArenaCard as C
from mtg_companion.match_review import _combat_clear
class CompactTests(unittest.TestCase):
    def test_empty_feed_provides_setup_without_inventing_a_board(self):
        result=analyze(G())
        self.assertEqual(result['headline'],'Waiting for Arena')
        self.assertEqual(result['items'],[])
    def test_blocking_analysis_distinguishes_flying_defenders(self):
        drake=C(1,1,'Wind Drake','Creature',2,2,attacking=True,abilities=('flying',))
        bear=C(2,2,'Grizzly Bears','Creature',2,2)
        reach=C(3,3,'Giant Spider','Creature',2,4,abilities=('reach',))
        s=G(available=True,detailed=True,in_match=True,my_seat=1,decision_player=1,request='Declare blockers',my_life=8,opponent_battlefield=(drake,),my_battlefield=(bear,reach))
        result=analyze(s)
        self.assertIn('2 total power',result['summary'])
        air=next(item for item in result['items'] if item['label']=='Watch the air')
        self.assertIn('Giant Spider',air['text']);self.assertNotIn('Grizzly Bears',air['text'])
    def test_unknown_card_data_prevents_combat_counterfactual(self):
        self.assertFalse(_combat_clear(C(1,1,'Unresolved','Creature',2,2,abilities=('unresolved rules text',))))
    def test_attackers_respect_haste_and_defender(self):
        haste=C(1,1,'Hasty','Creature',2,2,summoning_sick=True,abilities=('haste',))
        wall=C(2,2,'Wall','Creature',0,4,abilities=('defender',))
        s=G(available=True,detailed=True,in_match=True,request='Declare attackers',my_battlefield=(haste,wall))
        ready=analyze(s)['items'][0]['text'];self.assertIn('Hasty',ready);self.assertNotIn('Wall',ready)

class MoveChoiceTests(unittest.TestCase):
    def setUp(self):
        from compact import move_choices
        self.moves=move_choices
        self.state=G(available=True,detailed=True,in_match=True,my_seat=1,decision_player=1,request='You have priority')
    def test_opponents_decision_has_no_move_suggestion(self):
        result=self.moves(replace(self.state,decision_player=2,priority_player=2))
        self.assertEqual(result,dict(options=[],suggestion=None))
    def test_duplicate_card_copies_share_one_clear_choice(self):
        from mtg_companion.arena_state import ArenaAction as A
        land=A('ActionType_Play','Swamp','','')
        result=self.moves(replace(self.state,hand=(C(1,1,'Swamp','Basic Land'),),actions=(land,land)))
        self.assertEqual([o['title'] for o in result['options']],['Play Swamp','Pass priority'])
        self.assertIn('land play',result['options'][0]['consequence'])
        self.assertEqual(result['suggestion']['title'],'Pass priority')
    def test_expensive_timing_option_includes_payment_context(self):
        from mtg_companion.arena_state import ArenaAction as A
        result=self.moves(replace(self.state,actions=(A('ActionType_Cast','Serra Angel','{3}{W}{W}',''),),mana_sources_available=2))
        self.assertEqual(result['options'][0]['cost'],'{3}{W}{W}')
        self.assertIn('2 untapped',result['options'][0]['payment'])
        self.assertNotEqual(result['suggestion']['title'],'Cast Serra Angel')
    def test_no_blind_attack_all_suggestion(self):
        result=self.moves(replace(self.state,request='Declare attackers'))
        self.assertEqual(result['suggestion']['title'],'Choose attackers')
        self.assertEqual(len(result['options']),2)
    def test_unknown_prompt_does_not_invent_a_recommendation(self):
        self.assertIsNone(self.moves(replace(self.state,request='Unknown prompt'))['suggestion'])
    def test_current_damage_prompt_does_not_suggest_obsolete_blocker_order(self):
        result=self.moves(replace(self.state,request='Assign combat damage'))
        self.assertEqual(result['suggestion']['title'],'Assign combat damage')
    def test_unavailable_feed_never_shows_actions(self):
        self.assertEqual(self.moves(replace(self.state,detailed=False))['options'],[])
