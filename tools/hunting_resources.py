"""Observed resource balances, not user-entered spending budgets."""
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from game_calendar import game_day
from local_cache import save

TOKEN = re.compile(r'(\d+)/(\d+)(?:[|丨]?\+([\d,.]+)([KkMm]?))?')

def parse_balance(text):
    text = re.sub(r'\s+', '', text)
    match = TOKEN.fullmatch(text)
    if not match or int(match[2]) <= 0:
        return None
    extra = None; display = None; approximate = False
    if match[3] is not None:
        raw=match[3];suffix=match[4].upper()
        if suffix:
            if not re.fullmatch(r'\d+(?:\.\d+)?|\d+,\d{1,2}',raw):return None
            number=raw.replace(',','.')
        else:
            if not re.fullmatch(r'\d+|\d{1,3}(?:,\d{3})+',raw):return None
            number=raw.replace(',','')
        try:amount = Decimal(number)
        except InvalidOperation:return None
        scale = {'':1,'K':1000,'M':1000000}[suffix]
        approximate = bool(suffix)
        quantum = Decimal(10) ** amount.as_tuple().exponent
        # Abbreviated displays are approximate: use the conservative lower bound.
        extra = max(0, int((amount - quantum / 2) * scale)) if approximate else int(amount)
        display = number + suffix
    return {'free_available':int(match[1]), 'free_capacity':int(match[2]),
            'owned_extra':extra, 'extra_display':display, 'extra_approximate':approximate}

def read_balance(doc):
    lines = sorted(doc.within((1000,0,1275,65)), key=lambda row:row['box'][0])
    candidates = []
    for line in lines:
        match = re.search(r'\d+/\d+(?:[|丨]?\+[\d,.]+[KkMm]?)?',line['text'])
        if match:
            token = match[0]
            if '+' not in token:
                right = [row['text'] for row in lines if row['box'][0] >= line['box'][2] - 3 and abs(row['point'][1]-line['point'][1]) < 15 and row['text'].startswith('+')]
                if len(right)==1: token += right[0]
            value = parse_balance(token)
            if value and value not in candidates: candidates.append(value)
    return candidates[0] if len(candidates)==1 else None


def read_fixed_balance(image,output,ocr):
    import cv2,hashlib
    normalized=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
    tokens=[];output=Path(output);output.mkdir(parents=True,exist_ok=True)
    for name,box in (('free',(1078,22,1134,46)),('extra',(1139,22,1200,46))):
        a,b,c,d=box;gray=cv2.cvtColor(normalized[b:d,a:c],cv2.COLOR_BGR2GRAY)
        height=gray.shape[0]
        gray=cv2.copyMakeBorder(gray,8,8,85,8,cv2.BORDER_CONSTANT,value=0)
        cv2.putText(gray,'VALUE',(3,8+int(height*.8)),cv2.FONT_HERSHEY_SIMPLEX,.6,255,1,cv2.LINE_AA)
        encoded=cv2.imencode('.png',cv2.resize(gray,None,fx=3,fy=3,interpolation=cv2.INTER_CUBIC))[1]
        path=output/f'balance_{name}_{hashlib.sha256(encoded.tobytes()).hexdigest()[:16]}.png';encoded.tofile(str(path))
        payload=ocr(path,output/'balance_ocr',language='en-US')
        text=re.sub(r'\s+','',''.join(line['text'] for line in payload['lines']))
        tokens.append(text[5:] if text.startswith('VALUE') else '')
    if not re.fullmatch(r'\d+/\d+',tokens[0]):return None
    extra=tokens[1] if re.fullmatch(r'\+[\d.,]+[KkMm]?',tokens[1]) else ''
    return parse_balance(tokens[0]+extra)

def retain_observation(root, observation, image, cache_date):
    resource = observation.get('resource')
    if resource not in ('rice','torch') or type(observation.get('free_available')) is not int:
        return
    path = Path(root) / 'runtime/hunting_resources.json'
    try: data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    except (OSError,ValueError): data = {}
    if data.get('cache_date') != cache_date: data = {'cache_date':cache_date, 'resources':{}}
    data.setdefault('resources', {})[resource] = {key:observation.get(key) for key in
        ('free_available','free_capacity','owned_extra','extra_display','extra_approximate')}
    data['resources'][resource].update(observed_at=datetime.now(timezone.utc).isoformat(),image=str(image))
    inventory=observation.get('stone_inventory')
    if isinstance(inventory,dict) and len(inventory)==5 and all(type(value) is int and value>=0 for value in inventory.values()):
        data['stone_inventory']={'values':inventory,'observed_at':datetime.now(timezone.utc).isoformat(),'image':str(image)}
    save(path, data)


def stone_inventory_text(root):
    try:
        from daily_tasks import STONE_STAGES
        data=json.loads((Path(root)/'runtime/hunting_resources.json').read_text(encoding='utf-8'))
        values=data['stone_inventory']['values']
        if data.get('cache_date')!=game_day().isoformat() or set(values)!=set(STONE_STAGES):raise ValueError('unknown')
        target=min(STONE_STAGES,key=values.get)
        parts=' / '.join(name[0]+': '+str(values[name]) for name in STONE_STAGES)
        return '最近画面 '+parts+' → 最少：'+target+'（执行时重新核对）'
    except (OSError,KeyError,ValueError,TypeError):
        return '五种库存待读取；执行时两帧核对，并显示五项数量与选定属性。'

def resource_text(root, resource):
    try:
        data = json.loads((Path(root)/'runtime/hunting_resources.json').read_text(encoding='utf-8'))
        value = data['resources'][resource]
        if data.get('cache_date') != game_day().isoformat(): raise ValueError('stale game day')
        observed = datetime.fromisoformat(value['observed_at'])
        age = max(0, int((datetime.now(timezone.utc)-observed).total_seconds()))
        extra = ('约' if value.get('extra_approximate') else '') + value['extra_display'] if value.get('extra_display') is not None else '待识别'
        capacity = '/' + str(value['free_capacity']) if value.get('free_capacity') is not None else ''
        return f"免费 {value['free_available']}{capacity} · 额外 {extra} · {age}秒前画面"
    except (OSError,ValueError,KeyError,TypeError):
        return '待自动识别 · 狩猎执行时读取并持续刷新'
