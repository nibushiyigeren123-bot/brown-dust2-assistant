"""Guild entry and check-in receipt. Default is inspection; --execute is explicit."""
import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from local_cache import cache_path, load_local_config

from daily_tasks import guard_foreign_pending
from event_reward_rules import load_config
from m00_recognize import ROOT
from pending_daily import exclusive_ledger
from phase08_vision import Phase08Vision


class GuildController:
    def __init__(self, data):
        self.data=data
        self.dismissed=False
        self.back_clicked=False
        self.dismiss_attempts=0
        self.back_attempts=0
        self.review_reason=None

    def request_review_return(self, reason):
        self.review_reason=reason

    def step(self, obs):
        state=obs['state']
        if state=='guild_check_in_receipt':
            if self.dismiss_attempts>=3:
                return {'status':'needs_review','reason':'guild_receipt_did_not_close_after_three_attempts'}
            return {'status':'action','action':'dismiss_receipt','point':obs['points']['dismiss']}
        if state=='main_menu':
            if self.data.get('issued'):
                return {'status':'finished','reason':'check_in_receipt_and_menu_verified'}
            if self.review_reason:
                return {'status':'needs_review','reason':self.review_reason}
            if self.data.get('pending'):
                return {'status':'wait','reason':'waiting_for_guild_entry_transition'}
            if obs.get('guild_entry'):
                return {'status':'action','action':'enter_guild','point':obs['guild_entry']['point']}
            return {'status':'needs_review','reason':'guild_icon_not_recognized'}
        if state=='guild_page':
            if self.data.get('issued') or self.review_reason:
                if self.back_attempts<3:
                    return {'status':'action','action':'return_to_menu','point':obs['points']['back']}
                return {'status':'needs_review','reason':'guild_return_did_not_reach_menu_after_three_attempts'}
            # There is no demonstrated already-claimed UI. Do not infer done.
            return {'status':'wait','reason':'waiting_for_check_in_receipt'}
        return {'status':'wait','reason':'unrecognized_or_loading'}

    def commit(self,decision):
        action=decision['action']
        if action=='enter_guild':
            self.data['pending']={'module':'guild','phase':'entry_attempted'}
        elif action=='dismiss_receipt':
            self.data['issued']=True
            self.data['pending']={'module':'guild','phase':'receipt_seen_return_pending'}
            self.dismissed=True
            self.dismiss_attempts+=1
        elif action=='return_to_menu':
            self.back_clicked=True
            self.back_attempts+=1


def save_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    with temporary.open('w',encoding='utf-8',newline='\n') as handle:
        handle.write(json.dumps(data,ensure_ascii=False,indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary,path)


def run(args):
    config=load_config(args.config)
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=True)
    vision=Phase08Vision()
    report={'execute':args.execute,'completed':False,'actions':[],'observations':[]}
    if args.image:
        if args.execute:
            raise ValueError('An image inspection cannot execute')
        report['observation']=vision.inspect_file(args.image,output)
        save_json(output/'inspection.json',report)
        return report
    from game_window import GameWindow
    import win32api
    window=GameWindow.find()
    ledger=cache_path(ROOT, config, 'guild')
    def save():
        save_json(output/'run.json',report)
    with exclusive_ledger(ROOT/'runtime/game_control'):
        if args.execute:
            if not config['guild']['enabled']:
                raise RuntimeError('Guild module is disabled')
            guard_foreign_pending(ROOT/'runtime',ledger)
        data=json.loads(ledger.read_text(encoding='utf-8')) if args.execute and ledger.exists() else {
            'schema_version':1,'cache_date':config['cache_date'],
            'pending':None,'issued':False,'completed':False}
        if data['cache_date'] != config['cache_date']:
            raise RuntimeError('Guild ledger identity mismatch')
        controller=GuildController(data)
        from runtime_policy import network_wait_seconds
        network_grace = network_wait_seconds() if args.execute else 8
        deadline=time.monotonic()+max(args.timeout, network_grace * 2)
        guild_wait_since=None
        entry_transition_since=None
        try:
            while time.monotonic()<deadline:
                if win32api.GetAsyncKeyState(0x77)&0x8000:
                    report['stopped']='F8'
                    break
                image,geometry=window.capture()
                path=output/f"frame_{len(report['observations']):03}.png"
                image.save(path)
                obs=vision.inspect_file(path,output)
                decision=controller.step(obs)
                report['observations'].append({'image':str(path),'observation':obs,'decision':decision})
                save()
                if not args.execute:
                    break
                if decision['status']=='finished':
                    data['pending']=None
                    data['completed']=True
                    save_json(ledger,data)
                    report['completed']=True
                    report['returned_to_main_menu']=True
                    break
                if decision['status']=='needs_review':
                    report['returned_to_main_menu']=obs['state']=='main_menu'
                    report['stopped']=decision
                    break
                if decision['status']=='action':
                    # A fast auto-issued banner can appear during OCR. Re-read
                    # the UI before committing either an entry or a return.
                    fresh,fresh_geometry=window.capture()
                    fresh_path=output/f"preclick_{len(report['observations']):03}.png"
                    fresh.save(fresh_path)
                    current=vision.inspect_file(fresh_path,output)
                    candidate=controller.step(current)
                    if candidate!=decision:
                        continue
                    if win32api.GetAsyncKeyState(0x77)&0x8000:
                        report['stopped']='F8'
                        break
                    controller.commit(decision)
                    # Entry itself can issue the reward: persist before clicking.
                    save_json(ledger,data)
                    x,y=decision['point']
                    receipt=window.click_client([x*fresh_geometry['width']/1280,y*fresh_geometry['height']/720],fresh_geometry)
                    report['actions'].append({**decision,**receipt})
                    save()
                    time.sleep(0.9)
                if obs['state']=='main_menu' and data.get('pending') and not data.get('issued'):
                    entry_transition_since=entry_transition_since or time.monotonic()
                    if time.monotonic()-entry_transition_since>network_grace:
                        controller.request_review_return('entry_was_attempted_but_no_check_in_receipt')
                else:
                    entry_transition_since=None
                if obs['state']=='guild_page' and not data.get('issued'):
                    guild_wait_since=guild_wait_since or time.monotonic()
                    if time.monotonic()-guild_wait_since>network_grace:
                        controller.request_review_return('No receipt seen: already-signed/network/not-member states need review')
                time.sleep(0.25)
            if args.execute and not report['completed'] and 'stopped' not in report:
                report['stopped']='timeout_without_verified_check_in'
        except Exception as error:
            report['error']=str(error)
            raise
        finally:
            report['progress']=data
            save()
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'config/event_and_rewards.json')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--image',type=Path)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--timeout',type=float,default=40)
    args=parser.parse_args()
    result=run(args)
    print(json.dumps({k:result.get(k) for k in ('execute','completed','stopped','observation')},ensure_ascii=False))
    if args.execute and not result['completed']:
        raise SystemExit(1)


if __name__=='__main__':
    main()
