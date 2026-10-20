"""Retry navigation only after two observations still show its source page."""
import time
from game_window import check_requested_stop


def navigate(window,capture,point,source,accept,timeout,on_action,validate=None):
    deadline=time.monotonic()+timeout;attempts=0;previous=None;stable=0;recovered=False
    while time.monotonic()<deadline:
        check_requested_stop()
        obs,geo,path=capture()
        if accept(obs):return obs,geo,path
        signature=(obs.get('state'),obs.get('resource'),geo)
        stable=stable+1 if signature==previous else 1
        previous=signature
        if obs.get('state')==source and stable>=2:
            if attempts>=3:raise RuntimeError('导航点击三次后仍为原页面，停止核对，不继续盲点。')
            if attempts==2 and not recovered:
                recovery=window.recover_input(geo)
                recovered=True;previous=None;stable=0
                on_action({'input_kind':'native_minimize_restore',**recovery},path,attempts)
                continue
            window.focus_client(geo)
            fresh,fresh_geo,fresh_path=capture()
            if accept(fresh):return fresh,fresh_geo,fresh_path
            if (fresh.get('state'),fresh.get('resource'),fresh_geo)!=signature:
                previous=None;stable=0;continue
            if validate is not None and not validate(fresh, fresh_geo, fresh_path):
                # A modal/toggle animation can still expose its source title.
                # Re-observe; never retry an input whose target is unverified.
                previous=None;stable=0;time.sleep(.15);continue
            event=window.click_client([point[0]*geo['width']/1280,point[1]*geo['height']/720],geo,press_ms=250)
            attempts+=1;on_action(event,fresh_path,attempts)
            previous=None;stable=0
        time.sleep(.15)
    raise RuntimeError(f'等待导航页面响应已达{timeout:g}秒，停止核对。')
