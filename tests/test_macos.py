import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from mtg_companion import arena_cards, arena_state
from test_arena import _full_state_message, _gre_line, _StubCardDatabase
from test_match_review import _state, _game_over

ROOT=Path(__file__).resolve().parents[1]
ENGINE=ROOT/'Resources/engine'
class MacReaderTests(unittest.TestCase):
    def test_discovers_standard_macos_log(self):
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp)
            path=home/'Library/Logs/Wizards Of The Coast/MTGA/Player.log'
            path.parent.mkdir(parents=True);path.write_text('test')
            with patch.object(Path,'home',return_value=home),patch.dict(os.environ,{},clear=True):
                self.assertEqual(arena_state.find_player_log(),path)

    def test_explicit_missing_log_does_not_fall_back_to_unrelated_game(self):
        with patch.dict(os.environ,{'MTGA_PLAYER_LOG':'/nonexistent/Player.log'}):
            self.assertIsNone(arena_state.find_player_log())

    def test_partial_line_is_retained_until_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            log=Path(temp)/'Player.log';payload=_gre_line(_full_state_message())+'\n'
            log.write_text(payload[:50])
            reader=arena_state.ArenaLogReader()
            with patch.dict(os.environ,{'MTGA_PLAYER_LOG':str(log)}),patch('mtg_companion.arena_state.load_card_database',return_value=_StubCardDatabase()):
                self.assertFalse(reader.poll().detailed)
                with log.open('a') as f:f.write(payload[50:])
                state=reader.poll();self.assertTrue(state.in_match);self.assertEqual(state.my_life,6)

    def test_replaced_log_with_same_name_resets_state(self):
        with tempfile.TemporaryDirectory() as temp:
            log=Path(temp)/'Player.log';log.write_text(_gre_line(_full_state_message())+'\n')
            reader=arena_state.ArenaLogReader()
            with patch.dict(os.environ,{'MTGA_PLAYER_LOG':str(log)}),patch('mtg_companion.arena_state.load_card_database',return_value=_StubCardDatabase()):
                self.assertEqual(reader.poll().my_life,6)
                message=_full_state_message();message['gameStateMessage']['players'][0]['lifeTotal']=19
                replacement=Path(temp)/'new.log';replacement.write_text(_gre_line(message)+'\n'+' '*200+'\n');replacement.replace(log)
                self.assertEqual(reader.poll().my_life,19)

    def test_database_missing_rules_text_is_unknown(self):
        self.assertIn('unresolved',arena_state._ability_texts({},None)[0])
        card=arena_state.ArenaCard(1,1,'Unresolved','Creature',2,2,abilities=arena_state._ability_texts({},None))
        self.assertTrue(card.unreadable_abilities)

    def test_database_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'cards.mtga'
            connection=sqlite3.connect(path);connection.execute('CREATE TABLE sentinel (value TEXT)');connection.commit();connection.close()
            database=arena_cards.CardDatabase(path)
            with self.assertRaises(sqlite3.OperationalError):database._connection.execute("INSERT INTO sentinel VALUES ('changed')")
            database.close()

class EngineProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.data=Path(self.temp.name)
        # Keep protocol tests independent of any real Arena match on this Mac.
        log=self.data/'Player.log';log.write_text('')
        (self.data/'settings.json').write_text(json.dumps({'log_path':str(log)}))
        env=dict(os.environ,ARCANA_DATA_DIR=str(self.data))
        self.process=subprocess.Popen([sys.executable,'-I','-B',str(ENGINE/'launch.py')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
    def tearDown(self):
        self.process.stdin.close();self.process.wait(timeout=10)
        self.assertEqual(self.process.returncode,0,self.process.stderr.read())
        self.process.stdout.close();self.process.stderr.close();self.temp.cleanup()
    def call(self,action,**fields):
        self.process.stdin.write(json.dumps(dict(id=1,action=action,**fields))+'\n');self.process.stdin.flush()
        response=json.loads(self.process.stdout.readline());self.assertTrue(response['ok'],response)
        return response['result']
    def test_demo_search_and_memory_survive_independent_process(self):
        demo=self.call('demo',enabled=True);self.assertTrue(demo['demo']);self.assertEqual(demo['state']['my_life'],18)
        self.assertEqual(demo['stats']['games'],0)
        self.assertIn('DEMO',self.call('ask',query='What am I allowed to do?')['answer'])
        self.assertTrue(self.call('search',query='priority')['hits'])
        self.call('ask',query='Remember that I prefer blue control decks')
        self.assertIn('prefer blue',self.call('ask',query='blue control')['answer'])
        self.assertEqual(self.call('snapshot')['notes_count'],1)
        self.call('demo',enabled=False);self.assertFalse(self.call('snapshot')['demo'])
    def test_finished_game_persists_once_across_reconnect(self):
        log=self.data/'Player.log'
        messages=[_state(3,'Phase_Main1','',[],full=True),_game_over(won=True)]
        log.write_text(_gre_line(*messages)+'\n')
        result=self.call('configure',key='log_path',path=str(log))
        self.assertEqual(result['stats']['games'],1)
        self.assertEqual(result['stats']['wins'],1)
        self.assertIn('test-match',json.dumps(result['history']))
        again=self.call('configure',key='log_path',path=str(log))
        self.assertEqual(again['stats']['games'],1)
        self.assertTrue(list((self.data/'history').glob('*.json')))
    def test_imported_rules_search_is_local_and_removable(self):
        rules=self.data/'my-rules.txt'
        rules.write_text('702.2 A synthetic imported rule for the test.\n702.3 A second fixture rule.\n')
        self.call('configure',key='rules_path',path=str(rules))
        hit=self.call('search',query='702.2')['hits'][0]
        self.assertEqual(hit['locator'],'702.2')
        self.assertIn('synthetic imported',hit['text'])
        self.call('configure',key='rules_path',path='')
        self.assertFalse(self.call('search',query='702.2')['hits'])
        self.assertTrue(rules.is_file())

    def test_recording_mode_withholds_coaching_but_practice_can_enable_it(self):
        log=self.data/'Player.log'
        log.write_text(_gre_line(_state(3,'Phase_Main1','',[],full=True))+'\n')
        result=self.call('configure',key='log_path',path=str(log))
        self.assertTrue(result['state']['in_match'])
        self.assertFalse(result['coaching'])
        self.assertFalse(result['analysis'].get('suggestion'))
        self.assertFalse(result['analysis'].get('options'))
        self.assertIn('noncompetitive practice',self.call('ask',query='What should I do?')['answer'])
        self.assertTrue(self.call('practice')['coaching'])
        self.assertFalse(self.call('practice')['coaching'])

    def test_background_records_consecutive_games_with_distinct_timelines(self):
        import time
        log=self.data/'Player.log'
        self.call('practice')
        messages=[]
        for number,turn in [(1,3),(2,5),(3,7)]:
            begin=_state(turn,'Phase_Main1','',[],full=True)
            end=_game_over(won=number!=2)
            for message in [begin,end]:
                message['gameStateMessage']['gameInfo']['gameNumber']=number
            messages.extend([begin,end])
        log.write_text(_gre_line(*messages)+'\n')
        # No configure/poll command: the background watcher must discover all games.
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            result=self.call('snapshot')
            if result['stats']['games']==3:break
            time.sleep(.25)
        self.assertEqual(result['stats']['games'],3)
        self.assertEqual(result['stats']['wins'],2)
        self.assertFalse(result['practice'])
        for row in result['history']:
            self.assertEqual(row['timeline'][0]['turn'],{1:3,2:5,3:7}[row['game_number']])
        again=self.call('configure',key='log_path',path=str(log))
        self.assertEqual(again['stats']['games'],3)

    def test_invalid_database_and_unknown_action_return_errors(self):
        self.process.stdin.write(json.dumps({'id':2,'action':'configure','key':'database_path','path':'/nonexistent'})+'\n');self.process.stdin.flush()
        response=json.loads(self.process.stdout.readline());self.assertFalse(response['ok'])
        self.assertNotIn('database_path',self.call('snapshot')['settings'])

if __name__=='__main__':unittest.main()
