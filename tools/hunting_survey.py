"""Navigation-only resource survey. Never opens quick hunt, claims or buys."""
from pathlib import Path
from frame_store import next_path
import time
from daily_vision import DailyVision, inspect_frame
from hunting_resources import retain_observation
from game_calendar import check_execution_day
from game_window import check_requested_stop
from runtime_policy import network_wait_seconds, wait_observed


def survey_resources(window, config, output, root, on_frame=None):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    vision=DailyVision(); sequence=0; actions=[]; resources={}
    def capture():
        nonlocal sequence
        check_execution_day(config['cache_date']);check_requested_stop()
        image,geo=window.capture();path=output/f'frame_{sequence:03d}.png';sequence+=1
        path=next_path(path);image.save(path)
        obs=inspect_frame(path,vision,config,output/'ocr')
        if on_frame:on_frame(obs,path)
        return obs,geo,path
    def click(point,expected,label,target):
        if label not in ('open_hunting','select_hunting_category','cancel_hunt_dialog'):
            raise RuntimeError('资源总览只允许导航。')
        from navigation_input import navigate
        return navigate(window,capture,point,expected,target,network_wait_seconds(root),
                        lambda event,path,attempt:actions.append({'type':label,'evidence':str(path),'attempt':attempt,**event}))
    obs,geo,path=capture()
    if obs['state']=='hunt_dialog':
        obs,geo,path=click(obs['points']['cancel'],'hunt_dialog','cancel_hunt_dialog',lambda o:o['state']=='hunt_map')
    if obs['state']=='main_menu' and obs.get('hunt_entry_point'):
        obs,geo,path=click(obs['hunt_entry_point'],'main_menu','open_hunting',lambda o:o['state']=='hunt_map')
    if obs['state']!='hunt_map':raise RuntimeError('读取狩猎资源前未识别地图。')
    for resource,category in (('rice','狩猎场'),('torch','圣石洞穴')):
        if obs.get('resource')!=resource:
            point=obs.get('category_points',{}).get(category)
            if not point:raise RuntimeError('未读清资源分类入口：'+category)
            obs,geo,path=click(point,'hunt_map','select_hunting_category',lambda o:o['state']=='hunt_map' and o.get('resource')==resource)
        def signature(o):
            return tuple(o.get(k) for k in ('resource','free_available','free_capacity','owned_extra','extra_display'))
        previous=obs
        for attempt in range(4):
            fresh,geo,path=capture()
            if fresh['state']=='hunt_map' and fresh.get('resource')==resource and type(fresh.get('free_available')) is int and signature(fresh)==signature(previous):
                retain_observation(root,fresh,path,config['cache_date']);resources[resource]=fresh;obs=fresh
                break
            previous=fresh
        else:raise RuntimeError('未连续读清资源余额：'+resource)
    result={'resources':resources,'actions':actions,'completed':True,'consumes':False}
    from local_cache import save
    save(output/'run.json',result)
    return result
