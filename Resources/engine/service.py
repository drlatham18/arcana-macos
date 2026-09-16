"""Arcana's offline engine. JSON lines over private stdin/stdout; no network server."""
from __future__ import annotations
import hashlib
import json
import os
import re
import sys
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path

from compact import analyze, move_choices
from mtg_companion import arena_cards, arena_state, knowledge, match_review, memory, offline

DATA = Path(os.environ.get('ARCANA_DATA_DIR', Path.home() / 'Library/Application Support/Arcana'))
DATA.mkdir(parents=True, exist_ok=True)
knowledge.INDEX_PATH = DATA / 'knowledge.sqlite3'
memory.MEMORY_PATH = DATA / 'memory.sqlite3'
match_review.REVIEW_DIR = DATA / 'reviews'
SETTINGS = DATA / 'settings.json'
LOCK = threading.RLock()

def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)

def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return default

class Companion:
    def __init__(self):
        self.settings = read_json(SETTINGS, {})
        self.demo = False
        self.paused = False
        self.practice = False
        self.timelines = {}
        self.demo_step = 0
        self.state = arena_state.ArenaGameState(reason='Looking for Arena…')
        self.error = ''
        self.last_poll = None
        self.timeline = []
        self.last_signature = None
        self.last_game = None
        self.apply_settings()
        self.reviewer = match_review.MatchReviewer()
        match_review.REVIEWER = self.reviewer
        self.reader = arena_state.ArenaLogReader(self.observe)
        self.library_count, _ = knowledge.build_index()

    def apply_settings(self):
        for key, env in [('log_path', 'MTGA_PLAYER_LOG'), ('database_path', 'MTGA_CARD_DB')]:
            value = self.settings.get(key, '')
            if value:
                os.environ[env] = value
            else:
                os.environ.pop(env, None)
        arena_cards._located = None
        knowledge.EXTRA_RULES_FILE = Path(self.settings['rules_path']) if self.settings.get('rules_path') else None

    def observe(self, state):
        self.reviewer.observe(state)
        if not state.in_match:
            return
        game = (state.match_id, state.game_number)
        if game != self.last_game:
            self.timeline = []
            self.last_signature = None
            self.last_game = game
            self.timelines[game] = self.timeline
            while len(self.timelines) > 100:
                self.timelines.pop(next(iter(self.timelines)))
        signature = (state.turn_number, state.phase, state.step, state.my_life, state.opponent_life)
        if signature != self.last_signature:
            self.timeline.append({'turn': state.turn_number, 'phase': arena_state.describe_phase(state.phase, state.step), 'you': state.my_life, 'opponent': state.opponent_life})
            del self.timeline[:-100]
            self.last_signature = signature

    def poll(self):
        with LOCK:
            if self.paused:
                return
            try:
                self.state = self.reader.poll()
                self.last_poll = time.time()
                self.error = ''
                for review in self.reviewer.pop_pending():
                    row = asdict(review)
                    row['markdown'] = review.to_markdown()
                    row['counts'] = review.counts()
                    identity = f'{review.match_id}:{review.game_number}' if review.match_id else f'{review.finished_at}:{review.game_number}'
                    row['id'] = hashlib.sha256(identity.encode()).hexdigest()[:24]
                    row['timeline'] = list(self.timelines.get((review.match_id, review.game_number), []))
                    self.practice = False
                    atomic_json(DATA / 'history' / (row['id'] + '.json'), row)
            except Exception as exc:
                self.error = f'Arena could not be read: {type(exc).__name__}: {exc}'

    def demo_state(self):
        from mtg_companion.arena_state import ArenaCard as C, ArenaAction as A, ArenaGameState as G
        combat = self.demo_step % 2 == 1
        return G(available=True, detailed=True, in_match=True, my_seat=1, my_name='You', opponent_name='Sparring partner', my_life=18, opponent_life=14 if not combat else 11, turn_number=5 if not combat else 6, phase='Phase_Main1' if not combat else 'Phase_Combat', step='' if not combat else 'Step_DeclareBlock', active_player=1 if not combat else 2, priority_player=1, decision_player=1, request='You have priority' if not combat else 'Declare blockers', hand=(C(1,0,'Serra Angel','Creature — Angel',4,4,abilities=('flying','vigilance')), C(2,0,'Pacifism','Enchantment — Aura'), C(3,0,'Plains','Basic Land — Plains')), my_battlefield=(C(4,0,'Air Elemental','Creature — Elemental',4,4,abilities=('flying',)),C(5,0,'Grizzly Bears','Creature — Bear',2,2),*(C(10+i,0,'Plains','Basic Land — Plains',tapped=i>2) for i in range(5))), opponent_battlefield=(C(6,0,'Hill Giant','Creature — Giant',3,3,attacking=combat),C(7,0,'Wind Drake','Creature — Drake',2,2,abilities=('flying',),attacking=combat)), stack=(), my_library=42,opponent_library=43,opponent_hand=4,my_graveyard=2,opponent_graveyard=1,mana_sources_available=3,actions=() if combat else (A('ActionType_Play','Plains','','Land play; confirm remaining land plays in Arena.'),A('ActionType_Cast','Pacifism','{1}{W}','Enchant creature')),match_id='DEMO',game_number=1)

    def reviews(self):
        files = sorted((DATA / 'history').glob('*.json'), key=lambda p:p.stat().st_mtime, reverse=True)[:100] if (DATA / 'history').exists() else []
        return [item for item in (read_json(p,{}) for p in files) if item.get('id')]

    def snapshot(self):
        state = self.demo_state() if self.demo else self.state
        game = asdict(state)
        game['log_path'] = str(state.log_path) if state.log_path else ''
        game.update(my_turn=state.my_turn, waiting_on_me=state.waiting_on_me, phase_label=arena_state.describe_phase(state.phase,state.step), summary=state.summary)
        database = arena_cards.find_card_database()
        coaching = self.demo or self.practice
        study_state = state if coaching or not state.in_match else arena_state.ArenaGameState(reason='Background recording is active. Coaching is for noncompetitive practice only.')
        explanation = offline.explain_visible_state('MTGA', '', '', arena=study_state)
        history = self.reviews()
        recent = history[:20]
        wins = sum('won' in r.get('result','').lower() for r in recent)
        losses = sum('lost' in r.get('result','').lower() for r in recent)
        try:
            modified = state.log_path.stat().st_mtime if state.log_path else None
        except OSError:
            modified = None
        return {'practice':self.practice, 'coaching':coaching, 'analysis':{**analyze(study_state), **move_choices(study_state)},'connection':{'available':self.state.available,'log_path':str(self.state.log_path or '')},'state':game,'demo':self.demo,'paused':self.paused,'demo_step':self.demo_step,'error':self.error,
            'explanation':asdict(explanation),'last_poll':self.last_poll,'log_modified':modified,
            'database':str(database) if database else '', 'library_count':self.library_count,
            'knowledge_date':Path(self.settings['rules_path']).name if self.settings.get('rules_path') and Path(self.settings['rules_path']).is_file() else 'Original study guides · September 2026', 'settings':self.settings,
            'history':history,'stats':{'games':len(history),'wins':wins,'losses':losses},
            'timeline':[] if self.demo else self.timeline,'data_dir':str(DATA),
            'notes_count':memory.memory_stats()[1]}

    def request(self, command):
        action = command.get('action','snapshot')
        if action == 'snapshot':
            return self.snapshot()
        if action == 'practice':
            self.practice = not self.practice
            return self.snapshot()
        if action == 'demo':
            self.demo = bool(command.get('enabled', not self.demo))
            return self.snapshot()
        if action == 'demo_next':
            self.demo_step += 1
            return self.snapshot()
        if action == 'pause':
            self.paused = not self.paused
            return self.snapshot()
        if action == 'configure':
            key = command.get('key')
            if key not in ('log_path','database_path','rules_path'):
                raise ValueError('Unknown setting')
            value = command.get('path','')
            if value and not Path(value).is_file():
                raise ValueError('Choose an existing file.')
            if key == 'database_path' and value:
                db = arena_cards.CardDatabase(Path(value))
                try:
                    db._connection.execute('SELECT GrpId, TitleId FROM Cards LIMIT 1')
                    db._connection.execute('SELECT LocId, Loc, Formatted FROM Localizations_enUS LIMIT 1')
                finally:
                    db.close()
            if key == 'rules_path' and value:
                file = Path(value)
                if file.stat().st_size > 8_000_000 or not any(re.match(r'^\d{3}\.\d+', line) for line in file.read_text(encoding='utf-8-sig', errors='replace').splitlines()):
                    raise ValueError('Choose a Comprehensive Rules text file from Wizards, up to 8 MB.')
            self.settings[key] = value
            self.apply_settings()
            atomic_json(SETTINGS,self.settings)
            if key == 'rules_path':
                self.library_count, _ = knowledge.build_index(force=True)
                return self.snapshot()
            self.reviewer = match_review.MatchReviewer()
            match_review.REVIEWER = self.reviewer
            self.reader = arena_state.ArenaLogReader(self.observe)
            self.demo = False
            self.timeline = []
            self.last_signature = None
            self.poll()
            return self.snapshot()
        if action == 'search':
            query = str(command.get('query',''))[:500]
            return {'hits':[asdict(hit) for hit in knowledge.search_knowledge(query,limit=8)]}
        if action == 'ask':
            query = str(command.get('query','')).strip()[:2000]
            if not query:
                raise ValueError('Write a question first.')
            prefix = re.match(r'^(?:remember that|correction:|remember:)\s*(.+)', query, re.I|re.S)
            if prefix:
                memory.teach(prefix.group(1),'Personal note')
                return {'answer':'Saved in your personal notes. Related questions can bring it back. Your note does not change the official rules.'}
            if 'review' in query.lower() and ('last' in query.lower() or 'match' in query.lower()):
                history = self.reviews()
                return {'answer':history[0]['markdown'] if history else 'No finished games yet. Keep Arcana running while you play; the completed review will appear in Match history.'}
            state = self.demo_state() if self.demo else self.state
            if not self.demo and (self.paused or (state.log_path and time.time()-state.log_path.stat().st_mtime>120)):
                state = arena_state.ArenaGameState(reason='The game feed is paused or has not updated recently. Resume watching and check Arena before using live guidance.')
            recording_only = state.in_match and not (self.demo or self.practice)
            if recording_only:
                state = arena_state.ArenaGameState(reason='Live coaching is off. Recording for postgame study continues.')
            answer = offline.answer_question(query,'MTGA','','',arena=state)
            if recording_only:
                answer = 'Postgame recording is active. Live coaching is for noncompetitive practice only; enable Practice coaching in Connection for a practice game.\n\n' + answer
            related = memory.recall(query,2)
            if related:
                answer += '\n\nYour personal notes (not official rules):\n'+'\n'.join('• '+n.note for n in related)
            if self.demo:
                answer = 'DEMO GAME · Illustrative data\n\n' + answer
            return {'answer':answer}
        raise ValueError('Unknown command')

def main():
    app = Companion()
    stop = threading.Event()
    def watch():
        while not stop.is_set():
            app.poll()
            stop.wait(2)
    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        for line in sys.stdin:
            request = {}
            try:
                request = json.loads(line)
                with LOCK:
                    response = {'id':request.get('id'), 'ok':True, 'result':app.request(request)}
            except Exception as exc:
                response = {'id':request.get('id'), 'ok':False, 'error':str(exc)}
            print(json.dumps(response,ensure_ascii=False,default=str),flush=True)
    finally:
        stop.set()

if __name__ == '__main__':
    main()
