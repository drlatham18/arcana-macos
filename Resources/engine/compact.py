"""Concise, phase-aware analysis for the floating companion."""
from mtg_companion.arena_state import describe_phase

def card_list(cards):
    return ', '.join(c.describe() for c in cards) or 'None shown'

def analyze(s):
    items=[]
    note='Based on the recorded board. Arena confirms legal actions and costs.'
    if not s.available or not s.detailed:
        return dict(headline='Waiting for Arena',summary='Enable Detailed Logs in Arena, restart the game, and keep this window beside it.',items=[],note='Expand Arcana for the connection guide.')
    if not s.in_match:
        return dict(headline='Ready for your next game',summary=s.result or 'Your next game’s analysis will appear here automatically.',items=[],note='Completed reviews are in Match history.')
    heading=s.request or ('You have priority' if s.waiting_on_me else 'Opponent’s decision')
    summary=s.prompt or ('You can make a decision in Arena.' if s.waiting_on_me else 'Waiting for your opponent. Watch for the next prompt.')
    if s.stack:
        items.append(dict(label='On the stack',text=card_list(s.stack),tone='purple'))
    if s.request=='Declare blockers':
        attackers=[c for c in s.opponent_battlefield if c.attacking]
        defenders=[c for c in s.my_battlefield if c.is_creature and not c.tapped]
        power=sum(max(0,c.power or 0) for c in attackers)
        summary=f'{len(attackers)} attackers show {power} total power. You have {s.my_life} life.'
        items.append(dict(label='Incoming',text=card_list(attackers),tone='rose'))
        items.append(dict(label='Untapped defenders',text=card_list(defenders),tone='green'))
        flyers=[c for c in attackers if 'flying' in c.keywords]
        if flyers:
            capable=[c for c in defenders if c.keywords & {'flying','reach'}]
            items.append(dict(label='Watch the air',text=f"{', '.join(c.name for c in flyers)} has flying. Your flying/reach defenders: {', '.join(c.name for c in capable) or 'none shown'}.",tone='gold'))
        if any(c.unreadable_abilities for c in attackers+defenders):
            note='Some rules text is unresolved or complex. These are visible candidates, not guaranteed legal blocks.'
        else:
            note='Blocking does not tap creatures. Power is not guaranteed damage; effects can change the exchange.'
    elif s.request=='Declare attackers':
        candidates=[c for c in s.my_battlefield if c.is_creature and not c.tapped and 'defender' not in c.keywords and (not c.summoning_sick or 'haste' in c.keywords)]
        defenders=[c for c in s.opponent_battlefield if c.is_creature and not c.tapped]
        items.append(dict(label='Ready creatures',text=card_list(candidates),tone='green'))
        items.append(dict(label='Potential opposing blockers',text=card_list(defenders),tone='rose'))
        note='Check restrictions and attack costs in Arena. You may declare no attackers.'
    elif s.request=='Keep or mulligan':
        lands=sum(c.is_land for c in s.hand)
        summary=f'{len(s.hand)} cards in hand; {lands} identified as lands.'
        items.append(dict(label='Opening hand',text=card_list(s.hand),tone='purple'))
        note='Check your colors, early plays, and deck’s mana needs before keeping.'
    elif s.waiting_on_me:
        if s.actions:
            items.append(dict(label='Timing options',text='\n'.join(a.label() for a in s.actions[:4]),tone='purple'))
            items.append(dict(label='Mana available',text=f'{s.mana_sources_available} untapped sources shown. Check colors and additional costs in Arena.',tone='gold'))
        elif not s.stack:
            items.append(dict(label='Current prompt',text=s.prompt or 'Answer the highlighted prompt in Arena. Passing priority lets the game continue.',tone='purple'))
    elif not s.stack:
        attackers=[c for c in s.opponent_battlefield if c.attacking]
        if attackers:items.append(dict(label='Attacking creatures',text=card_list(attackers),tone='rose'))
        else:items.append(dict(label='Board at a glance',text=f'{len(s.my_battlefield)} permanents on your side · {len(s.opponent_battlefield)} opposing permanents · {len(s.hand)} cards in your hand.',tone='purple'))
    return dict(headline=heading,summary=summary,items=items,note=note)


def move_choices(s):
    """Presentation-ready choices; a suggestion is guidance, not a game command."""
    if not (s.available and s.detailed and s.in_match and s.waiting_on_me):
        return dict(options=[], suggestion=None)
    from mtg_companion.actions import ARENA_RESPONSES, ARENA_RECOMMENDATIONS
    options=[]
    seen=set()
    for action in s.actions:
        title=f'{action.verb} {action.card}'.strip()
        key=(title,action.mana_cost,action.detail)
        if key in seen:
            continue
        seen.add(key)
        verb=action.verb.casefold()
        card=next((c for c in (*s.hand,*s.my_battlefield) if c.name==action.card),None)
        if verb=='play' and card and card.is_land:
            consequence='Use a land play to put this land onto the battlefield.'
        elif verb=='cast':
            consequence='Pay costs and select required targets. Players can respond before the spell resolves.'
        elif verb=='activate':
            consequence='Pay the ability’s costs and make its required choices in Arena.'
        elif verb=='pass':
            consequence='Give up priority. If everyone passes, the top stack object resolves or the game advances.'
        else:
            consequence=action.detail or 'Make this choice in Arena and follow its next prompt.'
        options.append(dict(title=title,cost=action.mana_cost,consequence=consequence,
                            detail=action.detail,
                            payment=(f'{s.mana_sources_available} untapped sources shown · check payment' if action.mana_cost else '')))
    responses=list(ARENA_RESPONSES.get(s.request,[]))
    if s.request=='Declare attackers':
        responses=[('Choose attackers','Select eligible creatures, then confirm the attack. Check the opposing blockers first.'),
                   ('Attack with none','Keep your creatures back and move past the attack declaration.')]
    elif s.request=='Assign combat damage':
        responses=[('Assign combat damage','Choose damage assignments to the blocking creatures as Arena prompts, then confirm.')]
    elif s.request=='Declare blockers':
        responses=[('Assign blockers','Choose which creatures block each attacker, then confirm. Blocking does not tap them.'),
                   ('Take the damage','Confirm no blocks. Unblocked attackers can deal combat damage to what they are attacking.')]
    for title,consequence in responses:
        if title=='Pass priority':
            consequence='Give up priority. If everyone passes, the top stack object resolves or the game advances.'
        if any(o['title']==title for o in options):
            continue
        options.append(dict(title=title,cost='',consequence=consequence,detail='',payment=''))
    recommended=ARENA_RECOMMENDATIONS.get(s.request,'')
    recommended={'Attack with all':'Choose attackers','Order the blockers':'Assign combat damage'}.get(recommended,recommended)
    selected=next((o for o in options if o['title']==recommended),None)
    # Do not invent a strategic best play from an unranked engine action list.
    suggestion=dict(title=selected['title'],reason=selected['consequence']) if selected else None
    return dict(options=options,suggestion=suggestion)
